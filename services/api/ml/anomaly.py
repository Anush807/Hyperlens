"""Isolation Forest anomaly detection over hourly windows (spec 9.2)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import settings
from ml.data import EVIDENCE_COLUMNS, FEATURE_COLUMNS, feature_matrix

log = logging.getLogger(__name__)

ARTIFACT_SUBDIR = "anomaly"
MODEL_FILE = "isolation_forest.joblib"
META_FILE = "meta.json"

# Share of training windows allowed to be flagged, which sets the score cutoff.
#
# This is the precision/recall dial. The injected anomaly windows in the
# synthetic scenarios cover roughly 0.2% of all hourly windows, so anything
# much above that buys recall with a flood of false positives - at 1% the
# measured F1 was 0.10. Tune it with `--flag-rate` and re-run `make evaluate`.
DEFAULT_FLAG_RATE = 0.008

# A flagged window must also deviate by at least this many robust standard
# deviations on some signal.
#
# This gate exists because Isolation Forest scores isolation in the *joint*
# distribution, not magnitude: measured against ground truth, 90% of the real
# anomalies deviated by 33 sigma or more, while 90% of the false positives
# deviated by 7 or less - and the forest ranked many of the latter above the
# former. The forest still decides what is unusual; this only refuses to call
# something an anomaly that no operator would accept as one. It is also the
# part of the decision that can be explained in a sentence.
MIN_DEVIATION = 10.0


@dataclass
class AnomalyModel:
    """A fitted pipeline plus the score cutoff derived from training scores."""

    pipeline: Pipeline
    threshold: float
    min_deviation: float = MIN_DEVIATION
    feature_columns: list[str] = field(default_factory=lambda: list(FEATURE_COLUMNS))
    trained_at: datetime | None = None
    train_windows: int = 0
    version: int = 1

    def score(self, df: pd.DataFrame) -> np.ndarray:
        """Lower score = more anomalous (sklearn's decision_function convention)."""
        return self.pipeline.decision_function(feature_matrix(df))

    def predict_flags(self, df: pd.DataFrame) -> np.ndarray:
        return (self.score(df) < self.threshold) & self.passes_deviation_gate(df)

    def passes_deviation_gate(self, df: pd.DataFrame) -> np.ndarray:
        columns = [c for c in self.feature_columns if c in df.columns]
        if not columns:
            return np.ones(len(df), dtype=bool)
        return df[columns].abs().max(axis=1).to_numpy() >= self.min_deviation


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "forest",
                IsolationForest(
                    n_estimators=200,
                    contamination="auto",
                    random_state=42,
                    n_jobs=-1,
                ),
            ),
        ]
    )


def train(
    df: pd.DataFrame,
    *,
    flag_rate: float = DEFAULT_FLAG_RATE,
    min_deviation: float = MIN_DEVIATION,
) -> AnomalyModel:
    if df.empty:
        raise ValueError("no hourly windows to train on - run `make seed` first")
    pipeline = build_pipeline()
    features = feature_matrix(df)
    pipeline.fit(features)

    # The cutoff is the flag_rate-th percentile of training scores rather than
    # sklearn's own offset_, so alert volume is a tunable rather than a surprise.
    scores = pipeline.decision_function(features)
    threshold = float(np.quantile(scores, flag_rate))

    model = AnomalyModel(
        pipeline=pipeline,
        threshold=threshold,
        min_deviation=min_deviation,
        trained_at=datetime.now(UTC),
        train_windows=int(len(df)),
    )
    log.info(
        "trained isolation forest",
        extra={"windows": len(df), "threshold": round(threshold, 5), "flag_rate": flag_rate},
    )
    return model


def score_all(model: AnomalyModel, df: pd.DataFrame) -> pd.DataFrame:
    """Every window with its score and flag - the input to window-level metrics."""
    if df.empty:
        return df
    out = df.copy()
    out["score"] = model.score(out)
    out["max_deviation"] = (
        out[[c for c in model.feature_columns if c in out.columns]].abs().max(axis=1)
    )
    out["is_anomaly"] = (out["score"] < model.threshold) & model.passes_deviation_gate(out)
    return out


def detect(model: AnomalyModel, df: pd.DataFrame) -> pd.DataFrame:
    """Just the flagged windows, oldest first."""
    if df.empty:
        return df
    scored = score_all(model, df)
    return scored[scored["is_anomaly"]].sort_values("window_start")


# Minimum flagged hours before a run becomes an incident. Left at 1 on purpose:
# raising it to 2 was measured and it cost more true positives than false ones,
# because a real multi-hour incident often has only its worst hour flagged.
MIN_WINDOWS_PER_INCIDENT = 1


def merge_windows(
    flagged: pd.DataFrame,
    *,
    gap_hours: int = 1,
    min_windows: int = MIN_WINDOWS_PER_INCIDENT,
) -> list[dict[str, Any]]:
    """Collapse consecutive flagged hours for one entity into a single incident.

    Without this a six-hour latency storm becomes six separate alerts.
    """
    if flagged.empty:
        return []
    incidents: list[dict[str, Any]] = []
    gap = pd.Timedelta(hours=gap_hours)

    for entity_id, group in flagged.groupby("entity_external_id", sort=False):
        group = group.sort_values("window_start")
        current: dict[str, Any] | None = None
        for _, row in group.iterrows():
            start = row["window_start"]
            end = start + pd.Timedelta(hours=1)
            if current and start - current["window_end"] <= gap:
                current["window_end"] = max(current["window_end"], end)
                current["score"] = min(current["score"], float(row["score"]))
                current["windows"] += 1
                current["features"] = _features_of(row)
            else:
                if current:
                    incidents.append(current)
                current = {
                    "entity_external_id": entity_id,
                    "window_start": start,
                    "window_end": end,
                    "score": float(row["score"]),
                    "windows": 1,
                    "features": _features_of(row),
                }
        if current:
            incidents.append(current)

    return [i for i in incidents if i["windows"] >= min_windows]


def _features_of(row: pd.Series) -> dict[str, float]:
    """Everything an operator needs to judge the alert, not just what was scored."""
    return {col: round(float(row[col]), 3) for col in EVIDENCE_COLUMNS if col in row.index}


# --- Persistence ----------------------------------------------------------


def artifact_dir() -> Path:
    path = Path(settings.ml_artifact_dir) / ARTIFACT_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(model: AnomalyModel) -> Path:
    import joblib

    directory = artifact_dir()
    joblib.dump(model.pipeline, directory / MODEL_FILE)
    (directory / META_FILE).write_text(
        json.dumps(
            {
                "threshold": model.threshold,
                "min_deviation": model.min_deviation,
                "feature_columns": model.feature_columns,
                "trained_at": (model.trained_at or datetime.now(UTC)).isoformat(),
                "train_windows": model.train_windows,
                "version": model.version,
            },
            indent=2,
        )
    )
    log.info("saved anomaly artifacts", extra={"dir": str(directory)})
    return directory


def load() -> AnomalyModel | None:
    import joblib

    directory = artifact_dir()
    model_path = directory / MODEL_FILE
    meta_path = directory / META_FILE
    if not model_path.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    return AnomalyModel(
        pipeline=joblib.load(model_path),
        threshold=float(meta["threshold"]),
        min_deviation=float(meta.get("min_deviation", MIN_DEVIATION)),
        feature_columns=meta.get("feature_columns", list(FEATURE_COLUMNS)),
        trained_at=datetime.fromisoformat(meta["trained_at"]),
        train_windows=int(meta.get("train_windows", 0)),
        version=int(meta.get("version", 1)),
    )


# --- Evaluation -----------------------------------------------------------


def window_labels(
    scored: pd.DataFrame,
    ground_truth: list[dict[str, Any]],
) -> tuple[pd.Series, pd.Series]:
    """Per-window (predicted, actual) labels.

    An hourly window is *actually* anomalous when its hour falls inside an
    injected window for that same entity.
    """
    truth_by_entity: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for item in ground_truth:
        truth_by_entity.setdefault(item["entity_external_id"], []).append(
            (_as_utc(item["start"]), _as_utc(item["end"]))
        )

    starts = pd.DatetimeIndex(scored["window_start"])
    ends = starts + pd.Timedelta(hours=1)

    actual = []
    for entity_id, start, end in zip(scored["entity_external_id"], starts, ends, strict=True):
        spans = truth_by_entity.get(entity_id, ())
        actual.append(any(start < t_end and end > t_start for t_start, t_end in spans))

    return scored["is_anomaly"].astype(bool), pd.Series(actual, index=scored.index)


def _as_utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")


def ablation(
    scored: pd.DataFrame, ground_truth: list[dict[str, Any]], model: AnomalyModel
) -> dict[str, dict[str, float]]:
    """Score the forest and the deviation gate separately as well as together.

    Reported rather than kept quiet: on this synthetic data the gate is doing
    the discriminating, and anyone reading the F1 deserves to know that before
    they trust the number on real metrics.
    """
    _, actual = window_labels(scored, ground_truth)
    forest = scored["score"].to_numpy() < model.threshold
    gate = scored["max_deviation"].to_numpy() >= model.min_deviation

    out: dict[str, dict[str, float]] = {}
    for name, predicted in (
        ("isolation_forest_only", forest),
        ("deviation_gate_only", gate),
        ("both", forest & gate),
    ):
        frame = scored.assign(is_anomaly=predicted)
        out[name] = score_windows(frame, ground_truth)
    return out


def score_windows(scored: pd.DataFrame, ground_truth: list[dict[str, Any]]) -> dict[str, float]:
    """Window-level precision / recall / F1 - the metric spec 9.2 specifies."""
    if scored.empty:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    predicted, actual = window_labels(scored, ground_truth)
    true_positives = int((predicted & actual).sum())
    false_positives = int((predicted & ~actual).sum())
    false_negatives = int((~predicted & actual).sum())

    precision = true_positives / (true_positives + false_positives) if predicted.any() else 0.0
    recall = true_positives / (true_positives + false_negatives) if actual.any() else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positive_windows": true_positives,
        "false_positive_windows": false_positives,
        "false_negative_windows": false_negatives,
        "anomalous_windows": int(actual.sum()),
        "flagged_windows": int(predicted.sum()),
    }


def score_against_ground_truth(
    incidents: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    tolerance_hours: int = 1,
    scored_from: pd.Timestamp | None = None,
    scored_to: pd.Timestamp | None = None,
) -> dict[str, float]:
    """Incident-level precision / recall, reported alongside the window-level
    numbers because it is what alert volume actually feels like.

    A detection counts as a true positive when it overlaps an injected window
    for the same entity, within `tolerance_hours` on either side.

    `scored_from` / `scored_to` restrict the ground truth to the period that was
    actually scored. Without them, detecting over 30 days while scoring against
    180 days of injected windows caps recall at an arbitrary fraction and the
    number means nothing.
    """
    truth = [
        (
            g["entity_external_id"],
            pd.Timestamp(g["start"]).tz_convert("UTC")
            if pd.Timestamp(g["start"]).tzinfo
            else pd.Timestamp(g["start"]).tz_localize("UTC"),
            pd.Timestamp(g["end"]).tz_convert("UTC")
            if pd.Timestamp(g["end"]).tzinfo
            else pd.Timestamp(g["end"]).tz_localize("UTC"),
        )
        for g in ground_truth
    ]
    if scored_from is not None or scored_to is not None:
        low = scored_from if scored_from is not None else pd.Timestamp.min.tz_localize("UTC")
        high = scored_to if scored_to is not None else pd.Timestamp.max.tz_localize("UTC")
        # Fully contained, not merely overlapping: an injected window whose last
        # hour clips the edge of the scored period has no scored rows to detect,
        # and counting it against recall measures nothing.
        truth = [t for t in truth if t[1] >= low and t[2] <= high]

    tolerance = pd.Timedelta(hours=tolerance_hours)

    matched_truth: set[int] = set()
    true_positives = 0
    for incident in incidents:
        start = pd.Timestamp(incident["window_start"])
        end = pd.Timestamp(incident["window_end"])
        hit = False
        for idx, (entity, t_start, t_end) in enumerate(truth):
            if entity != incident["entity_external_id"]:
                continue
            if start <= t_end + tolerance and end >= t_start - tolerance:
                matched_truth.add(idx)
                hit = True
        if hit:
            true_positives += 1

    detected = len(incidents)
    precision = true_positives / detected if detected else 0.0
    recall = len(matched_truth) / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "incident_precision": round(precision, 4),
        "incident_recall": round(recall, 4),
        "incident_f1": round(f1, 4),
        "detected": detected,
        "injected": len(truth),
        "matched": len(matched_truth),
    }
