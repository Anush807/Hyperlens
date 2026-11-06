"""In-process registry for loaded ML artifacts and last-run bookkeeping.

The API holds the models in memory; POST /ml/reload swaps them without a restart
after a training run writes new files to the shared volume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class MLState:
    anomaly_model: Any = None
    forecast_artifacts: Any = None
    loaded_at: datetime | None = None
    last_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    running: bool = False

    def reload(self) -> dict[str, Any]:
        """Re-read artifacts from ML_ARTIFACT_DIR. Missing artifacts are not an
        error - the API runs fine before the first training pass."""
        from ml import anomaly, forecast

        self.anomaly_model = anomaly.load()
        self.forecast_artifacts = forecast.load()
        self.loaded_at = datetime.now(UTC)
        status = {
            "anomaly_model": self.anomaly_model is not None,
            "forecast_model": self.forecast_artifacts is not None,
            "loaded_at": self.loaded_at.isoformat(),
        }
        log.info("ml artifacts reloaded", extra=status)
        return status

    def record_run(self, results: dict[str, Any]) -> None:
        self.last_runs.update(results)

    @property
    def preferred_forecast_model(self) -> str:
        """Which forecast the UI should lead with.

        Spec 14, Phase 5 is explicit that if the LSTM does not beat the
        seasonal-naive baseline, the baseline ships as the default. So the
        choice is read from the measured holdout rather than assumed: the LSTM
        leads only when it actually won, and with no artifact at all the
        baseline is the only thing there is.
        """
        artifacts = self.forecast_artifacts
        if artifacts is None:
            return "naive"
        holdout = (artifacts.metrics or {}).get("holdout") or {}
        return "lstm" if holdout.get("lstm_beats_naive") else "naive"

    def snapshot(self) -> dict[str, Any]:
        anomaly_meta: dict[str, Any] = {"loaded": self.anomaly_model is not None}
        if self.anomaly_model is not None:
            anomaly_meta.update(
                {
                    "threshold": round(self.anomaly_model.threshold, 6),
                    "train_windows": self.anomaly_model.train_windows,
                    "trained_at": (
                        self.anomaly_model.trained_at.isoformat()
                        if self.anomaly_model.trained_at
                        else None
                    ),
                }
            )
        anomaly_meta["last_run"] = self.last_runs.get("anomaly")

        forecast_meta: dict[str, Any] = {"loaded": self.forecast_artifacts is not None}
        if self.forecast_artifacts is not None:
            metrics = self.forecast_artifacts.metrics or {}
            forecast_meta.update(
                {
                    "version": self.forecast_artifacts.version,
                    "metric": self.forecast_artifacts.metric_name,
                    "trained_at": (
                        self.forecast_artifacts.trained_at.isoformat()
                        if self.forecast_artifacts.trained_at
                        else None
                    ),
                    "holdout": metrics.get("holdout"),
                    "by_profile": metrics.get("by_profile"),
                }
            )
        forecast_meta["last_run"] = self.last_runs.get("forecast")

        forecast_meta["preferred_model"] = self.preferred_forecast_model

        return {
            "anomaly": anomaly_meta,
            "forecast": forecast_meta,
            "rightsizing": {"last_run": self.last_runs.get("rightsizing")},
            "loaded_at": self.loaded_at,
            "running": self.running,
        }


state = MLState()
