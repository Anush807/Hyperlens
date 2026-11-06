"""Loading and shaping metric history for the models (spec 9.1).

Everything here is pandas-in / pandas-out and takes a Motor database so the same
functions run from the CLI and from the API's scheduled jobs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.mongo import METRICS, METRICS_DAILY

log = logging.getLogger(__name__)

DAILY_VALUE_COLUMNS = [
    "cpu_pct_mean",
    "cpu_pct_p95",
    "cpu_pct_max",
    "mem_pct_mean",
    "mem_pct_p95",
    "mem_pct_max",
    "disk_iops_mean",
    "disk_latency_ms_mean",
    "disk_latency_ms_p95",
    "net_kbps_mean",
]

MAX_FFILL_DAYS = 2
MAX_MISSING_FRACTION = 0.10


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Hourly windows hold ~12 samples, so interpolation
    would invent precision the data does not have."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return 0.0
    clean.sort()
    idx = max(0, min(len(clean) - 1, int(round(q / 100 * (len(clean) - 1)))))
    return round(clean[idx], 3)


async def load_daily(
    mongo: AsyncIOMotorDatabase,
    *,
    entity_type: str = "vm",
    days: int = 180,
    end: datetime | None = None,
) -> pd.DataFrame:
    """One row per entity per day, indexed by (entity_external_id, date).

    Gaps of up to 2 days are forward-filled; entities missing more than 10% of
    days are dropped, because an LSTM trained on interpolated noise is worse
    than no forecast for that entity.
    """
    end = end or datetime.now(UTC)
    start = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    cursor = mongo[METRICS_DAILY].find(
        {"entity_type": entity_type, "date": {"$gte": start, "$lte": end}}, {"_id": 0}
    )
    rows = [doc async for doc in cursor]
    if not rows:
        log.warning("no daily metrics found", extra={"entity_type": entity_type, "days": days})
        return pd.DataFrame(columns=["entity_external_id", "date", *DAILY_VALUE_COLUMNS])

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    for col in DAILY_VALUE_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    df = df[["entity_external_id", "date", *DAILY_VALUE_COLUMNS]]
    return _regularize(df, start=start, end=end)


def _regularize(df: pd.DataFrame, *, start: datetime, end: datetime) -> pd.DataFrame:
    """Reindex every entity onto a complete daily calendar, then clean."""
    calendar = pd.date_range(
        pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D", tz="UTC"
    )
    frames: list[pd.DataFrame] = []
    dropped = 0
    for entity_id, group in df.groupby("entity_external_id", sort=False):
        series = (
            group.drop_duplicates(subset="date", keep="last")
            .set_index("date")
            .reindex(calendar)
            .sort_index()
        )
        missing_fraction = series[DAILY_VALUE_COLUMNS[0]].isna().mean()
        if missing_fraction > MAX_MISSING_FRACTION:
            dropped += 1
            continue
        series[DAILY_VALUE_COLUMNS] = series[DAILY_VALUE_COLUMNS].ffill(limit=MAX_FFILL_DAYS)
        series = series.dropna(subset=[DAILY_VALUE_COLUMNS[0]])
        series["entity_external_id"] = entity_id
        series.index.name = "date"
        frames.append(series.reset_index())

    if dropped:
        log.info("dropped sparse entities", extra={"dropped": dropped})
    if not frames:
        return pd.DataFrame(columns=["entity_external_id", "date", *DAILY_VALUE_COLUMNS])
    out = pd.concat(frames, ignore_index=True)
    return out[["entity_external_id", "date", *DAILY_VALUE_COLUMNS]]


def pivot_metric(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Wide frame: index = date, one column per entity. The LSTM trains on this."""
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="date", columns="entity_external_id", values=metric).sort_index()


async def load_hourly_windows(
    mongo: AsyncIOMotorDatabase,
    *,
    days: int = 30,
    entity_type: str = "vm",
    end: datetime | None = None,
) -> pd.DataFrame:
    """Hourly feature windows for anomaly detection, built from raw points.

    Features per window (spec 9.2): mean/p95/std of cpu, mem and disk latency,
    mean iops and net, plus the delta against the same hour over the previous
    7 days - the delta is what stops a normal Monday-morning peak from scoring
    as an anomaly.
    """
    end = end or datetime.now(UTC)
    # Extra context so the first scored window already has a full baseline.
    start = end - timedelta(days=days + BASELINE_WARMUP_DAYS)

    pipeline: list[dict[str, Any]] = [
        {"$match": {"meta.entity_type": entity_type, "ts": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": {
                    "entity": "$meta.entity_external_id",
                    "hour": {"$dateTrunc": {"date": "$ts", "unit": "hour"}},
                },
                "cpu_mean": {"$avg": "$cpu_pct"},
                "cpu_std": {"$stdDevSamp": "$cpu_pct"},
                "cpu_values": {"$push": "$cpu_pct"},
                "mem_mean": {"$avg": "$mem_pct"},
                "mem_std": {"$stdDevSamp": "$mem_pct"},
                "mem_values": {"$push": "$mem_pct"},
                "lat_mean": {"$avg": "$disk_latency_ms"},
                "lat_std": {"$stdDevSamp": "$disk_latency_ms"},
                "lat_values": {"$push": "$disk_latency_ms"},
                "iops_mean": {"$avg": "$disk_iops"},
                "net_mean": {"$avg": "$net_kbps"},
                "samples": {"$sum": 1},
            }
        },
        {"$sort": {"_id.entity": 1, "_id.hour": 1}},
    ]

    rows = []
    async for doc in mongo[METRICS].aggregate(pipeline, allowDiskUse=True):
        rows.append(
            {
                "entity_external_id": doc["_id"]["entity"],
                "window_start": doc["_id"]["hour"],
                "cpu_mean": doc.get("cpu_mean") or 0.0,
                "cpu_std": doc.get("cpu_std") or 0.0,
                "cpu_p95": percentile(doc.get("cpu_values", []), 95),
                "mem_mean": doc.get("mem_mean") or 0.0,
                "mem_std": doc.get("mem_std") or 0.0,
                "mem_p95": percentile(doc.get("mem_values", []), 95),
                "lat_mean": doc.get("lat_mean") or 0.0,
                "lat_std": doc.get("lat_std") or 0.0,
                "lat_p95": percentile(doc.get("lat_values", []), 95),
                "iops_mean": doc.get("iops_mean") or 0.0,
                "net_mean": doc.get("net_mean") or 0.0,
                "samples": doc.get("samples", 0),
            }
        )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["window_start"] = pd.to_datetime(df["window_start"], utc=True)
    df = add_derived_features(df)
    # Trim the warm-up now that the baselines have consumed it.
    df = df[df["window_start"] >= pd.Timestamp(end - timedelta(days=days))]
    return drop_partial_windows(df).reset_index(drop=True)


# An hour is only comparable to other hours if it was observed for the whole
# hour. The newest hour is always still filling, and the hours right after a
# backfill ends are covered by live polling only in part.
MIN_SAMPLE_RATIO = 0.5


def drop_partial_windows(df: pd.DataFrame, ratio: float = MIN_SAMPLE_RATIO) -> pd.DataFrame:
    """Discard hours observed for less than `ratio` of the usual sample count.

    A half-observed hour has a mean drawn from half a diurnal curve, which is a
    large deviation from its own baseline and nothing to do with an anomaly -
    before this filter, the current date alone produced a third of all false
    positives.
    """
    if df.empty or "samples" not in df.columns:
        return df
    typical = float(df["samples"].median())
    if typical <= 0:
        return df
    return df[df["samples"] >= typical * ratio]


# The raw signals spec 9.2 lists, before they are made entity-relative.
ABSOLUTE_COLUMNS = [
    "cpu_mean",
    "cpu_p95",
    "cpu_std",
    "mem_mean",
    "mem_p95",
    "mem_std",
    "lat_mean",
    "lat_p95",
    "lat_std",
    "iops_mean",
    "net_mean",
]

DELTA_BASE_COLUMNS = [
    "cpu_mean",
    "cpu_p95",
    "mem_mean",
    "lat_mean",
    "lat_p95",
    "iops_mean",
    "net_mean",
]

# How many earlier occurrences of the same hour-and-day-type form the baseline,
# and how they are combined. The median matters as much as the count: with a
# 3-occurrence mean, one injected anomaly shifted the baseline of the *next*
# three same-hour windows by a third of its own magnitude, so every real
# incident cast a shadow of false ones behind it. A median over five
# occurrences ignores a single contaminated sample outright.
BASELINE_OCCURRENCES = 5

# Days of history loaded before the scored window, so even weekend hours (two
# occurrences per week) have a full baseline on the first scored row.
BASELINE_WARMUP_DAYS = 35


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Turn raw hourly aggregates into the entity-relative features the model sees.

    Every feature is relative to the entity's own behaviour, and that is the
    single most important decision in this module. One global model scoring raw
    magnitudes across a heterogeneous estate learns "busy is weird": measured
    against ground truth it spent its whole budget re-flagging the handful of
    permanently hot VMs every hour (F1 0.10) and missed every injected memory
    leak and network flood, because those VMs' absolute numbers were
    unremarkable next to the hot ones.

    A permanently hot VM is a capacity problem, not an anomaly - the
    right-sizing rules already raise it as `upsize_cpu`. The detector's job is
    *change*, so the computed features are:

      z_x     - robust z-score against the entity's own 30-day distribution
      delta_x - difference from the same hour on each of the previous 7 days
                (spec 9.2: this is what stops a normal Monday peak scoring)
      ratio_x - the same comparison multiplicatively, because latency and IOPS
                move by factors, not by absolute milliseconds

    Only delta_x and ratio_x are *scored* - see FEATURE_COLUMNS below. The
    absolutes and z-scores are carried through because they are recorded on the
    anomaly row as the evidence an operator (and the LLM summariser) reads.
    """
    df = df.sort_values(["entity_external_id", "window_start"]).copy()
    grouped = df.groupby("entity_external_id")

    for col in ABSOLUTE_COLUMNS:
        median = grouped[col].transform("median")
        q1 = grouped[col].transform(lambda s: s.quantile(0.25))
        q3 = grouped[col].transform(lambda s: s.quantile(0.75))
        # IQR, not std: a series containing the anomaly should not have its own
        # spread inflated by it. The floor keeps flat series from exploding.
        spread = (q3 - q1).clip(lower=0.5)
        df[f"z_{col}"] = ((df[col] - median) / spread).clip(-30, 30)

    # The baseline is the mean of this entity's previous occurrences of the same
    # hour *and the same day type*. Splitting weekday from weekend is not a
    # refinement, it is the difference between a working detector and a broken
    # one: these VMs run at ~55% of weekday load at the weekend, so a baseline
    # that averages the previous 7 calendar days puts every Saturday hour tens
    # of points below its own baseline and every Monday hour above it. Two
    # sevenths of all windows then look more anomalous than a real memory leak.
    df["_hour"] = df["window_start"].dt.hour
    df["_is_weekend"] = df["window_start"].dt.dayofweek >= 5
    buckets = df.groupby(["entity_external_id", "_is_weekend", "_hour"], sort=False)

    for col in DELTA_BASE_COLUMNS:
        baseline = buckets[col].transform(
            lambda s: s.shift(1).rolling(BASELINE_OCCURRENCES, min_periods=2).median()
        )
        # Rows inside the warm-up have no baseline; 0 and 1 mean "unremarkable",
        # and load_hourly_windows trims them off anyway.
        df[f"delta_{col}"] = (df[col] - baseline).fillna(0.0)
        df[f"ratio_{col}"] = (df[col] / baseline.replace(0.0, np.nan)).fillna(1.0).clip(0, 20)

        # Studentise the deviation by how much this entity's own windows
        # normally deviate. Without it the deltas are in native units - degrees
        # of CPU percent next to thousands of kbps - and Isolation Forest,
        # which splits one randomly chosen feature at a time, dilutes a large
        # deviation in two dimensions across the twelve where the window looks
        # ordinary. One comparable number per signal isolates far better than
        # two incomparable ones.
        spread = grouped[f"delta_{col}"].transform(_mad).clip(lower=1e-6)
        floor = max(float(df[col].abs().median()) * 0.02, 1e-6)
        df[f"dev_{col}"] = (df[f"delta_{col}"] / spread.clip(lower=floor)).clip(-50, 50)

    return df.drop(columns=["_hour", "_is_weekend"])


def _mad(series: pd.Series) -> float:
    """Median absolute deviation, scaled to be comparable to a standard
    deviation. Robust by construction, so the handful of anomalous windows in
    the series cannot inflate the spread they are being measured against."""
    median = series.median()
    return float(1.4826 * (series - median).abs().median())


# Kept as an alias: the name the spec uses for this step.
add_weekly_deltas = add_derived_features


# What the Isolation Forest is actually trained on.
#
# Two features per signal, both measured against the same hour on each of the
# previous 7 days. Everything else was deliberately left out, and the reason is
# worth recording because it is not obvious:
#
#   * Absolute magnitudes make a global model learn "busy is weird". Measured
#     against ground truth that scored F1 0.10 - it re-flagged the hot VMs
#     hourly and missed every injected memory leak.
#   * Per-entity z-scores over a 30-day window fire on slow drift, so the
#     `growing` profile - a legitimate capacity trend, already surfaced by the
#     forecaster - dominated the alerts.
#   * Isolation Forest also degrades as irrelevant dimensions are added: a
#     point extreme in one feature out of twenty-five is rarely split on, so a
#     large single-signal deviation scores *less* anomalous than a row that is
#     mildly odd everywhere. Fewer, sharper features isolate better.
#
# These 14 are immune to drift and to cross-entity scale, which is what makes
# one global model defensible across a heterogeneous estate.
FEATURE_COLUMNS = [f"dev_{col}" for col in DELTA_BASE_COLUMNS]

# Recorded on the anomaly row as evidence, not scored.
EVIDENCE_COLUMNS = [
    *ABSOLUTE_COLUMNS,
    *[f"z_{col}" for col in ABSOLUTE_COLUMNS],
    *[f"delta_{col}" for col in DELTA_BASE_COLUMNS],
    *[f"ratio_{col}" for col in DELTA_BASE_COLUMNS],
    *FEATURE_COLUMNS,
]


def feature_matrix(df: pd.DataFrame) -> np.ndarray:
    return df[FEATURE_COLUMNS].to_numpy(dtype=np.float64)
