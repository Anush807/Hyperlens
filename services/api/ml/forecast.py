"""30-day capacity forecasting: a seasonal-naive baseline and a PyTorch LSTM.

The baseline is not decoration. The spec is explicit that the LSTM has to beat
seasonal naive to be worth shipping, so both models are trained, both are scored
on the same held-out window, and both are written to the database - the UI shows
them together and `ml-evaluation.md` records which won.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from app.config import settings

log = logging.getLogger(__name__)

ARTIFACT_SUBDIR = "forecast"
SEASON_DAYS = 7  # weekly seasonality: the business-hours pattern repeats weekly

LOOKBACK = 28
HORIZON = 30
HIDDEN_SIZE = 64
NUM_LAYERS = 2
DROPOUT = 0.2
BATCH_SIZE = 64
LEARNING_RATE = 1e-3
MAX_EPOCHS = 100
PATIENCE = 5
SEED = 42


# --- Baseline -------------------------------------------------------------


def seasonal_naive(
    history: np.ndarray, horizon: int = HORIZON, season: int = SEASON_DAYS
) -> np.ndarray:
    """Repeat the value from the same weekday one week back, rolling forward."""
    if history.size == 0:
        return np.zeros(horizon)
    if history.size < season:
        return np.full(horizon, float(history[-1]))
    window = history[-season:]
    return np.array([window[i % season] for i in range(horizon)], dtype=np.float64)


# --- Scaling --------------------------------------------------------------


@dataclass
class EntityScaler:
    """Per-entity min-max. A 90%-CPU VM and a 3%-CPU VM must not share a scale."""

    minimum: float
    maximum: float

    @property
    def span(self) -> float:
        span = self.maximum - self.minimum
        return span if span > 1e-6 else 1.0

    def transform(self, values: np.ndarray) -> np.ndarray:
        return (values - self.minimum) / self.span

    def inverse(self, values: np.ndarray) -> np.ndarray:
        return values * self.span + self.minimum

    def to_dict(self) -> dict[str, float]:
        return {"min": self.minimum, "max": self.maximum}

    @classmethod
    def fit(cls, values: np.ndarray) -> EntityScaler:
        return cls(minimum=float(np.nanmin(values)), maximum=float(np.nanmax(values)))


def _calendar_features(dates: pd.DatetimeIndex) -> np.ndarray:
    """day-of-week as sin/cos so Sunday and Monday stay adjacent."""
    dow = dates.dayofweek.to_numpy(dtype=np.float64)
    angle = 2 * math.pi * dow / 7.0
    return np.stack([np.sin(angle), np.cos(angle)], axis=1)


# --- Windowing ------------------------------------------------------------


@dataclass
class Windows:
    x: np.ndarray  # (n, LOOKBACK, 3)
    y: np.ndarray  # (n, HORIZON)
    entity_ids: list[str]
    end_indices: list[int]

    def __len__(self) -> int:
        return int(self.x.shape[0])


def build_windows(
    wide: pd.DataFrame,
    scalers: dict[str, EntityScaler],
    *,
    lookback: int = LOOKBACK,
    horizon: int = HORIZON,
    last_index: int | None = None,
) -> Windows:
    """Sliding windows across every entity column of a date-indexed frame.

    `last_index` caps the end of the *target* window, which is how the
    train/val/test split stays strictly time-ordered.

    The target is the **delta from the last observed value**, not the level.
    With levels, a min-max scaler fitted on the training window caps the model
    at the highest value it ever saw, so a steadily growing VM - exactly the
    profile forecasting exists for - can never be predicted above its own
    history. Predicting the change removes that ceiling; the architecture and
    inputs are unchanged.
    """
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    entities: list[str] = []
    ends: list[int] = []

    calendar = _calendar_features(pd.DatetimeIndex(wide.index))
    limit = last_index if last_index is not None else len(wide)

    for entity_id in wide.columns:
        series = wide[entity_id].to_numpy(dtype=np.float64)
        if np.isnan(series).any():
            series = pd.Series(series).ffill().bfill().to_numpy()
        if np.isnan(series).any():
            continue
        scaler = scalers[entity_id]
        scaled = scaler.transform(series)

        for start in range(0, limit - lookback - horizon + 1):
            input_end = start + lookback
            target_end = input_end + horizon
            if target_end > limit:
                break
            window = np.concatenate(
                [scaled[start:input_end, None], calendar[start:input_end]], axis=1
            )
            xs.append(window)
            ys.append(scaled[input_end:target_end] - scaled[input_end - 1])
            entities.append(str(entity_id))
            ends.append(input_end)

    if not xs:
        return Windows(np.empty((0, lookback, 3)), np.empty((0, horizon)), [], [])
    return Windows(np.stack(xs), np.stack(ys), entities, ends)


# --- Model ----------------------------------------------------------------


class LSTMForecaster(nn.Module):
    """Direct multi-horizon head: 30 outputs in one shot.

    Recursive single-step forecasting compounds its own error over 30 steps,
    which is exactly the horizon we care about, so the head is direct.
    """

    def __init__(
        self,
        input_size: int = 3,
        hidden_size: int = HIDDEN_SIZE,
        num_layers: int = NUM_LAYERS,
        horizon: int = HORIZON,
        dropout: float = DROPOUT,
    ) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.dropout = nn.Dropout(dropout)
        self.head = nn.Linear(hidden_size, horizon)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        output, _ = self.lstm(x)
        return self.head(self.dropout(output[:, -1, :]))


@dataclass
class ForecastArtifacts:
    model: LSTMForecaster
    scalers: dict[str, EntityScaler]
    residual_quantiles: dict[str, list[float]] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    metric_name: str = "cpu_pct_p95"
    version: int = 1
    trained_at: datetime | None = None


# --- Training -------------------------------------------------------------


def _to_loader(windows: Windows, *, shuffle: bool) -> DataLoader:
    dataset = TensorDataset(
        torch.tensor(windows.x, dtype=torch.float32), torch.tensor(windows.y, dtype=torch.float32)
    )
    return DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=shuffle)


def train_lstm(
    wide: pd.DataFrame,
    *,
    metric_name: str = "cpu_pct_p95",
    lookback: int = LOOKBACK,
    horizon: int = HORIZON,
    max_epochs: int = MAX_EPOCHS,
    patience: int = PATIENCE,
    device: str = "cpu",
) -> ForecastArtifacts:
    """Time-based split: last 30 days test, the 30 before that validation."""
    torch.manual_seed(SEED)
    np.random.seed(SEED)

    if wide.empty or len(wide) < lookback + horizon * 3:
        raise ValueError(
            f"need at least {lookback + horizon * 3} days of history to train "
            f"(have {len(wide)}); run `make seed --days 180`"
        )

    n_days = len(wide)
    test_start = n_days - horizon
    val_start = test_start - horizon

    # Scalers are fit on the training window only - fitting on the whole series
    # would leak the test range's magnitude into training.
    scalers = {
        str(col): EntityScaler.fit(wide[col].to_numpy(dtype=np.float64)[:val_start])
        for col in wide.columns
    }

    train_windows = build_windows(
        wide, scalers, lookback=lookback, horizon=horizon, last_index=val_start
    )
    val_windows = build_windows(
        wide, scalers, lookback=lookback, horizon=horizon, last_index=test_start
    )
    # Keep only windows whose target starts inside validation.
    val_windows = _slice_after(val_windows, min_end=val_start - horizon)

    if len(train_windows) == 0:
        raise ValueError("no training windows could be built from the available history")

    model = LSTMForecaster(horizon=horizon).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    criterion = nn.MSELoss()

    train_loader = _to_loader(train_windows, shuffle=True)
    val_loader = _to_loader(val_windows, shuffle=False) if len(val_windows) else None

    best_state: dict[str, torch.Tensor] | None = None
    best_loss = float("inf")
    bad_epochs = 0
    history: list[dict[str, float]] = []

    for epoch in range(max_epochs):
        model.train()
        train_loss = 0.0
        for batch_x, batch_y in train_loader:
            optimizer.zero_grad()
            loss = criterion(model(batch_x.to(device)), batch_y.to(device))
            loss.backward()
            optimizer.step()
            train_loss += float(loss) * batch_x.size(0)
        train_loss /= float(max(1, len(train_windows)))

        val_loss = train_loss
        if val_loader is not None:
            model.eval()
            total = 0.0
            with torch.no_grad():
                for batch_x, batch_y in val_loader:
                    total += float(
                        criterion(model(batch_x.to(device)), batch_y.to(device))
                    ) * batch_x.size(0)
            val_loss = total / float(max(1, len(val_windows)))

        history.append(
            {"epoch": epoch, "train_loss": round(train_loss, 6), "val_loss": round(val_loss, 6)}
        )

        if val_loss < best_loss - 1e-6:
            best_loss = val_loss
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            bad_epochs = 0
        else:
            bad_epochs += 1
            if bad_epochs >= patience:
                log.info("early stopping", extra={"epoch": epoch, "best_val_loss": best_loss})
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()

    residuals = _residual_quantiles(model, val_windows, scalers, device=device)
    artifacts = ForecastArtifacts(
        model=model,
        scalers=scalers,
        residual_quantiles=residuals,
        metric_name=metric_name,
        trained_at=datetime.now(UTC),
        metrics={
            "best_val_loss": round(best_loss, 6),
            "epochs_run": len(history),
            "train_windows": len(train_windows),
            "val_windows": len(val_windows),
            "history": history[-10:],
        },
    )
    return artifacts


def _slice_after(windows: Windows, *, min_end: int) -> Windows:
    keep = [i for i, end in enumerate(windows.end_indices) if end >= min_end]
    if not keep:
        return windows
    return Windows(
        windows.x[keep],
        windows.y[keep],
        [windows.entity_ids[i] for i in keep],
        [windows.end_indices[i] for i in keep],
    )


def _residual_quantiles(
    model: LSTMForecaster,
    windows: Windows,
    scalers: dict[str, EntityScaler],
    *,
    device: str = "cpu",
) -> dict[str, list[float]]:
    """10th/90th percentile of (actual - predicted) per horizon step.

    Deliberately simple: the spec rules out Bayesian uncertainty for V1, and
    empirical residual bands are honest about what the model actually got wrong.
    """
    if len(windows) == 0:
        return {"p10": [0.0] * HORIZON, "p90": [0.0] * HORIZON}
    with torch.no_grad():
        predictions = model(torch.tensor(windows.x, dtype=torch.float32).to(device)).cpu().numpy()

    residuals = np.zeros_like(predictions)
    for i, entity_id in enumerate(windows.entity_ids):
        scaler = scalers[entity_id]
        # x[:, -1, 0] is the last scaled input value the deltas are relative to.
        anchor = float(windows.x[i, -1, 0])
        actual = scaler.inverse(windows.y[i] + anchor)
        predicted = scaler.inverse(predictions[i] + anchor)
        residuals[i] = actual - predicted

    return {
        "p10": [round(float(v), 4) for v in np.quantile(residuals, 0.10, axis=0)],
        "p90": [round(float(v), 4) for v in np.quantile(residuals, 0.90, axis=0)],
    }


# --- Inference ------------------------------------------------------------


def predict(
    artifacts: ForecastArtifacts,
    entity_id: str,
    history: np.ndarray,
    history_dates: pd.DatetimeIndex,
    *,
    horizon: int = HORIZON,
    lookback: int = LOOKBACK,
) -> np.ndarray:
    """Point forecast in original units for one entity."""
    scaler = artifacts.scalers.get(entity_id)
    if scaler is None:
        scaler = EntityScaler.fit(history)
    tail = history[-lookback:]
    tail_dates = history_dates[-lookback:]
    if tail.size < lookback:
        padding = np.full(lookback - tail.size, tail[0] if tail.size else 0.0)
        tail = np.concatenate([padding, tail])
        tail_dates = pd.DatetimeIndex(
            list(
                pd.date_range(end=tail_dates[0], periods=lookback - len(tail_dates) + 1, freq="D")[
                    :-1
                ]
            )
            + list(tail_dates)
        )

    scaled_tail = scaler.transform(tail)
    window = np.concatenate(
        [scaled_tail[:, None], _calendar_features(pd.DatetimeIndex(tail_dates))], axis=1
    )
    with torch.no_grad():
        deltas = artifacts.model(torch.tensor(window[None, :, :], dtype=torch.float32)).numpy()[0]
    # The head emits changes relative to the last observed point; add it back.
    return scaler.inverse(deltas + scaled_tail[-1])[:horizon]


def to_points(
    values: np.ndarray,
    start_date: date,
    residuals: dict[str, list[float]] | None = None,
    *,
    clamp: tuple[float, float] | None = (0.0, 100.0),
) -> list[dict[str, Any]]:
    """Forecast rows in the JSONB shape the API stores: {date, value, lower, upper}."""
    p10 = (residuals or {}).get("p10") or [0.0] * len(values)
    p90 = (residuals or {}).get("p90") or [0.0] * len(values)
    points = []
    for i, value in enumerate(values):
        lower = float(value) + float(p10[min(i, len(p10) - 1)])
        upper = float(value) + float(p90[min(i, len(p90) - 1)])
        if clamp:
            low, high = clamp
            value = min(max(float(value), low), high)
            lower = min(max(lower, low), high)
            upper = min(max(upper, low), high)
        points.append(
            {
                "date": (start_date + timedelta(days=i + 1)).isoformat(),
                "value": round(float(value), 2),
                "lower": round(min(lower, float(value)), 2),
                "upper": round(max(upper, float(value)), 2),
            }
        )
    return points


def days_to_threshold(points: list[dict[str, Any]], threshold: float) -> int | None:
    """First forecast day whose central value crosses the threshold, else None."""
    for i, point in enumerate(points, start=1):
        if point["value"] >= threshold:
            return i
    return None


# --- Evaluation -----------------------------------------------------------


def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(actual - predicted)))


def smape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Symmetric MAPE in percent. Guarded so near-zero idle series cannot
    produce a division blow-up."""
    denominator = (np.abs(actual) + np.abs(predicted)) / 2.0
    safe = np.where(denominator < 1e-6, 1e-6, denominator)
    return float(np.mean(np.abs(actual - predicted) / safe) * 100.0)


def evaluate_holdout(
    artifacts: ForecastArtifacts,
    wide: pd.DataFrame,
    *,
    horizon: int = HORIZON,
    lookback: int = LOOKBACK,
    profiles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Score LSTM vs seasonal naive on the final `horizon` days, per profile."""
    if len(wide) < lookback + horizon:
        return {"error": "insufficient history for holdout evaluation"}

    split = len(wide) - horizon
    train_part = wide.iloc[:split]
    test_part = wide.iloc[split:]
    dates = pd.DatetimeIndex(train_part.index)

    per_entity: list[dict[str, Any]] = []
    for entity_id in wide.columns:
        history = train_part[entity_id].to_numpy(dtype=np.float64)
        actual = test_part[entity_id].to_numpy(dtype=np.float64)
        if np.isnan(history).any() or np.isnan(actual).any():
            continue
        lstm_pred = predict(
            artifacts, str(entity_id), history, dates, horizon=horizon, lookback=lookback
        )
        naive_pred = seasonal_naive(history, horizon=horizon)
        per_entity.append(
            {
                "entity": str(entity_id),
                "profile": (profiles or {}).get(str(entity_id), "unknown"),
                "lstm_mae": round(mae(actual, lstm_pred), 4),
                "lstm_smape": round(smape(actual, lstm_pred), 3),
                "naive_mae": round(mae(actual, naive_pred), 4),
                "naive_smape": round(smape(actual, naive_pred), 3),
            }
        )

    if not per_entity:
        return {"error": "no entities with complete holdout data"}

    frame = pd.DataFrame(per_entity)
    overall = {
        "entities": len(frame),
        "lstm_mae": round(float(frame["lstm_mae"].mean()), 4),
        "lstm_smape": round(float(frame["lstm_smape"].mean()), 3),
        "naive_mae": round(float(frame["naive_mae"].mean()), 4),
        "naive_smape": round(float(frame["naive_smape"].mean()), 3),
    }
    overall["lstm_beats_naive"] = bool(overall["lstm_mae"] < overall["naive_mae"])

    by_profile = (
        frame.groupby("profile")[["lstm_mae", "lstm_smape", "naive_mae", "naive_smape"]]
        .mean()
        .round(4)
        .to_dict(orient="index")
    )
    for profile, row in by_profile.items():
        row["lstm_beats_naive"] = bool(row["lstm_mae"] < row["naive_mae"])
        by_profile[profile] = row

    return {"overall": overall, "by_profile": by_profile, "per_entity": per_entity}


# --- Persistence ----------------------------------------------------------


def artifact_dir() -> Path:
    path = Path(settings.ml_artifact_dir) / ARTIFACT_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(artifacts: ForecastArtifacts, version: int = 1) -> Path:
    directory = artifact_dir()
    torch.save(artifacts.model.state_dict(), directory / f"lstm_v{version}.pt")
    (directory / f"scalers_v{version}.json").write_text(
        json.dumps(
            {
                "metric": artifacts.metric_name,
                "residual_quantiles": artifacts.residual_quantiles,
                "scalers": {k: v.to_dict() for k, v in artifacts.scalers.items()},
            },
            indent=2,
        )
    )
    (directory / f"metrics_v{version}.json").write_text(
        json.dumps(
            {
                "trained_at": (artifacts.trained_at or datetime.now(UTC)).isoformat(),
                "metric": artifacts.metric_name,
                **artifacts.metrics,
            },
            indent=2,
            default=str,
        )
    )
    (directory / "latest.json").write_text(json.dumps({"version": version}, indent=2))
    log.info("saved forecast artifacts", extra={"dir": str(directory), "version": version})
    return directory


def load(version: int | None = None) -> ForecastArtifacts | None:
    directory = artifact_dir()
    if version is None:
        latest = directory / "latest.json"
        if not latest.exists():
            return None
        version = int(json.loads(latest.read_text())["version"])

    weights = directory / f"lstm_v{version}.pt"
    scalers_path = directory / f"scalers_v{version}.json"
    if not weights.exists() or not scalers_path.exists():
        return None

    payload = json.loads(scalers_path.read_text())
    model = LSTMForecaster()
    model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    model.eval()

    metrics_path = directory / f"metrics_v{version}.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}

    return ForecastArtifacts(
        model=model,
        scalers={
            k: EntityScaler(minimum=v["min"], maximum=v["max"])
            for k, v in payload.get("scalers", {}).items()
        },
        residual_quantiles=payload.get("residual_quantiles", {}),
        metric_name=payload.get("metric", "cpu_pct_p95"),
        metrics=metrics,
        version=version,
        trained_at=datetime.fromisoformat(metrics["trained_at"])
        if metrics.get("trained_at")
        else None,
    )
