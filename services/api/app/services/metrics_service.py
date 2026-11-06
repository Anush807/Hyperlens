"""Metric reads and the Mongo rollup job.

Raw points live in the `metrics` time-series collection at 5-minute resolution.
Hourly is aggregated on demand; daily is materialised into `metrics_daily` by the
hourly scheduler job because the ML code reads only from there (spec 6.3).
"""

from __future__ import annotations

import logging
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from app.db.mongo import METRICS, METRICS_DAILY
from app.schemas.metrics import METRIC_FIELDS, MetricRow, MetricStats

log = logging.getLogger(__name__)


def _meta_filter(entity_type: str, external_id: str) -> dict[str, Any]:
    return {"meta.entity_type": entity_type, "meta.entity_external_id": external_id}


async def get_series(
    mongo: AsyncIOMotorDatabase,
    *,
    entity_type: str,
    external_id: str,
    from_ts: datetime,
    to_ts: datetime,
    resolution: str,
    max_points: int = 5000,
) -> list[MetricRow]:
    if resolution == "daily":
        return await _daily_rows(mongo, entity_type, external_id, from_ts, to_ts)
    if resolution == "hourly":
        return await _bucketed_rows(mongo, entity_type, external_id, from_ts, to_ts, unit="hour")
    return await _raw_rows(mongo, entity_type, external_id, from_ts, to_ts, max_points)


async def _raw_rows(
    mongo: AsyncIOMotorDatabase,
    entity_type: str,
    external_id: str,
    from_ts: datetime,
    to_ts: datetime,
    max_points: int,
) -> list[MetricRow]:
    cursor = (
        mongo[METRICS]
        .find({**_meta_filter(entity_type, external_id), "ts": {"$gte": from_ts, "$lte": to_ts}})
        .sort("ts", 1)
        .limit(max_points)
    )
    return [MetricRow(ts=d["ts"], **{f: d.get(f) for f in METRIC_FIELDS}) async for d in cursor]


def _bucket_stage(unit: str) -> dict[str, Any]:
    return {"$dateTrunc": {"date": "$ts", "unit": unit}}


def _agg_fields() -> dict[str, Any]:
    group: dict[str, Any] = {}
    for field in METRIC_FIELDS:
        group[field] = {"$avg": f"${field}"}
    group["cpu_values"] = {"$push": "$cpu_pct"}
    group["mem_values"] = {"$push": "$mem_pct"}
    group["cpu_pct_max"] = {"$max": "$cpu_pct"}
    group["mem_pct_max"] = {"$max": "$mem_pct"}
    return group


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[idx]


async def _bucketed_rows(
    mongo: AsyncIOMotorDatabase,
    entity_type: str,
    external_id: str,
    from_ts: datetime,
    to_ts: datetime,
    unit: str,
) -> list[MetricRow]:
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                **_meta_filter(entity_type, external_id),
                "ts": {"$gte": from_ts, "$lte": to_ts},
            }
        },
        {"$group": {"_id": _bucket_stage(unit), **_agg_fields()}},
        {"$sort": {"_id": 1}},
        {"$limit": 5000},
    ]
    rows: list[MetricRow] = []
    async for doc in mongo[METRICS].aggregate(pipeline):
        rows.append(
            MetricRow(
                ts=doc["_id"],
                **{f: _round(doc.get(f)) for f in METRIC_FIELDS},
                cpu_pct_p95=round(_p95(doc.get("cpu_values", [])), 2),
                mem_pct_p95=round(_p95(doc.get("mem_values", [])), 2),
                cpu_pct_max=_round(doc.get("cpu_pct_max")),
                mem_pct_max=_round(doc.get("mem_pct_max")),
            )
        )
    return rows


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


async def _daily_rows(
    mongo: AsyncIOMotorDatabase,
    entity_type: str,
    external_id: str,
    from_ts: datetime,
    to_ts: datetime,
) -> list[MetricRow]:
    """Served from the pre-aggregated collection, falling back to live
    aggregation when the rollup has not run yet."""
    cursor = (
        mongo[METRICS_DAILY]
        .find(
            {
                "entity_type": entity_type,
                "entity_external_id": external_id,
                "date": {"$gte": _as_day(from_ts), "$lte": _as_day(to_ts)},
            }
        )
        .sort("date", 1)
    )
    rows = [
        MetricRow(
            ts=d["date"],
            cpu_pct=d.get("cpu_pct_mean"),
            mem_pct=d.get("mem_pct_mean"),
            disk_iops=d.get("disk_iops_mean"),
            disk_latency_ms=d.get("disk_latency_ms_mean"),
            net_kbps=d.get("net_kbps_mean"),
            cpu_pct_p95=d.get("cpu_pct_p95"),
            mem_pct_p95=d.get("mem_pct_p95"),
            cpu_pct_max=d.get("cpu_pct_max"),
            mem_pct_max=d.get("mem_pct_max"),
        )
        async for d in cursor
    ]
    if rows:
        return rows
    return await _bucketed_rows(mongo, entity_type, external_id, from_ts, to_ts, unit="day")


def _as_day(value: datetime) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


async def rollup_daily(mongo: AsyncIOMotorDatabase, *, days: int = 2) -> int:
    """Recompute the last `days` days of `metrics_daily` from raw points.

    Re-running is safe: rows are upserted on (entity_type, entity_external_id,
    date), which is the unique index.
    """
    since = _as_day(datetime.now(UTC) - timedelta(days=days))
    pipeline: list[dict[str, Any]] = [
        {"$match": {"ts": {"$gte": since}}},
        {
            "$group": {
                "_id": {
                    "entity_type": "$meta.entity_type",
                    "entity_external_id": "$meta.entity_external_id",
                    "date": {"$dateTrunc": {"date": "$ts", "unit": "day"}},
                },
                "source_id": {"$first": "$meta.source_id"},
                "samples": {"$sum": 1},
                **{f"{f}_mean": {"$avg": f"${f}"} for f in METRIC_FIELDS},
                **{f"{f}_max": {"$max": f"${f}"} for f in METRIC_FIELDS},
                "cpu_values": {"$push": "$cpu_pct"},
                "mem_values": {"$push": "$mem_pct"},
                "lat_values": {"$push": "$disk_latency_ms"},
            }
        },
    ]
    ops: list[UpdateOne] = []
    async for doc in mongo[METRICS].aggregate(pipeline, allowDiskUse=True):
        key = doc["_id"]
        row = {
            "entity_type": key["entity_type"],
            "entity_external_id": key["entity_external_id"],
            "date": key["date"],
            "source_id": doc.get("source_id"),
            "samples": doc["samples"],
            "cpu_pct_p95": round(_p95(doc.get("cpu_values", [])), 3),
            "mem_pct_p95": round(_p95(doc.get("mem_values", [])), 3),
            "disk_latency_ms_p95": round(_p95(doc.get("lat_values", [])), 3),
        }
        for field in METRIC_FIELDS:
            row[f"{field}_mean"] = _round(doc.get(f"{field}_mean"))
            row[f"{field}_max"] = _round(doc.get(f"{field}_max"))
        ops.append(
            UpdateOne(
                {
                    "entity_type": row["entity_type"],
                    "entity_external_id": row["entity_external_id"],
                    "date": row["date"],
                },
                {"$set": row},
                upsert=True,
            )
        )
        if len(ops) >= 1000:
            await mongo[METRICS_DAILY].bulk_write(ops, ordered=False)
            ops = []
    if ops:
        await mongo[METRICS_DAILY].bulk_write(ops, ordered=False)
    log.info("daily rollup complete", extra={"days": days})
    return days


async def metric_stats(
    mongo: AsyncIOMotorDatabase,
    *,
    entity_type: str,
    external_id: str,
    metric: str,
    days: int,
) -> MetricStats:
    """mean / p95 / max / trend used by the assistant's get_metric_stats tool."""
    if metric not in METRIC_FIELDS:
        raise ValueError(f"unknown metric {metric!r}")
    since = _as_day(datetime.now(UTC) - timedelta(days=days))
    cursor = (
        mongo[METRICS_DAILY]
        .find(
            {
                "entity_type": entity_type,
                "entity_external_id": external_id,
                "date": {"$gte": since},
            },
            {
                "date": 1,
                f"{metric}_mean": 1,
                f"{metric}_max": 1,
                "cpu_pct_p95": 1,
                "mem_pct_p95": 1,
            },
        )
        .sort("date", 1)
    )
    means: list[float] = []
    maxes: list[float] = []
    async for doc in cursor:
        value = doc.get(f"{metric}_mean")
        if value is not None:
            means.append(float(value))
            maxes.append(float(doc.get(f"{metric}_max") or value))

    if not means:
        return MetricStats(
            metric=metric, days=days, mean=0.0, p95=0.0, max=0.0, trend_slope_per_day=0.0, samples=0
        )
    return MetricStats(
        metric=metric,
        days=days,
        mean=round(statistics.fmean(means), 2),
        p95=round(_p95(means), 2),
        max=round(max(maxes), 2),
        trend_slope_per_day=round(_slope(means), 4),
        samples=len(means),
    )


def _slope(values: list[float]) -> float:
    """Ordinary least squares slope against the day index."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = statistics.fmean(values)
    numerator = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    return numerator / denominator if denominator else 0.0


async def daily_frame_rows(
    mongo: AsyncIOMotorDatabase, *, entity_type: str, since: date, fields: list[str] | None = None
) -> list[dict[str, Any]]:
    """Flat rows for pandas; used by ml/data.py."""
    projection = {"_id": 0}
    cursor = mongo[METRICS_DAILY].find(
        {
            "entity_type": entity_type,
            "date": {"$gte": _as_day(datetime(since.year, since.month, since.day, tzinfo=UTC))},
        },
        projection,
    )
    rows = [doc async for doc in cursor]
    if fields:
        keep = {"entity_external_id", "date", "entity_type", *fields}
        rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
    return rows
