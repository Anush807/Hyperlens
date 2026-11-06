"""Glue between the models in ml/ and the database.

These are the functions POST /ml/run and the nightly scheduler call. Each one is
independent and idempotent so a partial failure can be retried.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from motor.motor_asyncio import AsyncIOMotorDatabase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import VM, Cluster, Datastore, Host
from app.db.models.enums import EntityType, ForecastModel, PowerState, RecommendationKind
from app.services import ml_service
from ml import anomaly, forecast, rightsizing
from ml.data import load_daily, load_hourly_windows, pivot_metric

log = logging.getLogger(__name__)

FORECAST_METRICS = ("cpu_pct_p95", "mem_pct_p95")


@dataclass
class JobResult:
    name: str
    started_at: datetime
    finished_at: datetime
    details: dict[str, Any]

    @property
    def duration_seconds(self) -> float:
        return round((self.finished_at - self.started_at).total_seconds(), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            **self.details,
        }


async def _vm_lookup(session: AsyncSession) -> dict[str, uuid.UUID]:
    rows = (
        await session.execute(select(VM.external_id, VM.id).where(VM.is_deleted.is_(False)))
    ).all()
    return {row[0]: row[1] for row in rows}


async def _vm_specs(session: AsyncSession) -> list[rightsizing.VMSpec]:
    rows = (
        await session.execute(
            select(VM.external_id, VM.name, VM.vcpus, VM.memory_mb, VM.power_state).where(
                VM.is_deleted.is_(False)
            )
        )
    ).all()
    return [
        rightsizing.VMSpec(
            external_id=external_id,
            name=name,
            vcpus=int(vcpus),
            memory_mb=int(memory_mb),
            power_state=str(power_state),
        )
        for external_id, name, vcpus, memory_mb, power_state in rows
    ]


# --- Anomalies ------------------------------------------------------------


async def run_anomaly_detection(
    session: AsyncSession, mongo: AsyncIOMotorDatabase, *, days: int = 1
) -> JobResult:
    """Score the last `days` of hourly windows with the stored model."""
    started = datetime.now(UTC)
    model = anomaly.load()
    if model is None:
        log.warning("anomaly model not trained; skipping detection")
        return JobResult(
            "anomaly", started, datetime.now(UTC), {"skipped": "no trained model", "created": 0}
        )

    windows = await load_hourly_windows(mongo, days=days)
    if windows.empty:
        return JobResult("anomaly", started, datetime.now(UTC), {"created": 0, "windows": 0})

    flagged = anomaly.detect(model, windows)
    incidents = anomaly.merge_windows(flagged)
    vm_ids = await _vm_lookup(session)

    created = 0
    updated = 0
    for incident in incidents:
        vm_id = vm_ids.get(incident["entity_external_id"])
        if vm_id is None:
            continue
        _, is_new = await ml_service.upsert_anomaly(
            session,
            entity_type=EntityType.VM,
            entity_id=vm_id,
            window_start=incident["window_start"].to_pydatetime(),
            window_end=incident["window_end"].to_pydatetime(),
            score=float(incident["score"]),
            features={**incident["features"], "flagged_windows": incident["windows"]},
        )
        created += int(is_new)
        updated += int(not is_new)

    await session.commit()
    return JobResult(
        "anomaly",
        started,
        datetime.now(UTC),
        {
            "windows": int(len(windows)),
            "flagged_windows": int(len(flagged)),
            "incidents": len(incidents),
            "created": created,
            "updated": updated,
        },
    )


# --- Forecasts ------------------------------------------------------------


async def run_forecasts(
    session: AsyncSession, mongo: AsyncIOMotorDatabase, *, days: int = 180
) -> JobResult:
    """Regenerate VM, cluster and datastore forecasts from the stored LSTM.

    Both models are always written. When no LSTM artifact exists the naive
    baseline still ships, which is the documented degraded mode.
    """
    started = datetime.now(UTC)
    artifacts = forecast.load()
    daily = await load_daily(mongo, entity_type="vm", days=days)
    if daily.empty:
        return JobResult("forecast", started, datetime.now(UTC), {"skipped": "no daily metrics"})

    vm_ids = await _vm_lookup(session)
    written = 0

    for metric in FORECAST_METRICS:
        wide = pivot_metric(daily, metric)
        if wide.empty:
            continue
        dates = pd.DatetimeIndex(wide.index)
        last_date = dates[-1].date()

        for entity_id in wide.columns:
            vm_id = vm_ids.get(str(entity_id))
            if vm_id is None:
                continue
            history = wide[entity_id].ffill().bfill().to_numpy(dtype=np.float64)
            if np.isnan(history).any():
                continue

            naive_values = forecast.seasonal_naive(history)
            written += await _store(
                session,
                EntityType.VM,
                vm_id,
                metric,
                ForecastModel.NAIVE,
                forecast.to_points(naive_values, last_date),
            )

            if artifacts is not None:
                lstm_values = forecast.predict(artifacts, str(entity_id), history, dates)
                written += await _store(
                    session,
                    EntityType.VM,
                    vm_id,
                    metric,
                    ForecastModel.LSTM,
                    forecast.to_points(lstm_values, last_date, artifacts.residual_quantiles),
                )

    written += await _cluster_forecasts(session, daily, artifacts)
    written += await _datastore_forecasts(session, mongo, artifacts)

    await session.commit()
    return JobResult("forecast", started, datetime.now(UTC), {"forecasts_written": written})


async def _store(
    session: AsyncSession,
    entity_type: EntityType,
    entity_id: uuid.UUID,
    metric: str,
    model: ForecastModel,
    points: list[dict[str, Any]],
) -> int:
    threshold = (
        settings.datastore_threshold_pct
        if entity_type is EntityType.DATASTORE
        else (
            settings.cpu_threshold_pct if metric.startswith("cpu") else settings.mem_threshold_pct
        )
    )
    await ml_service.save_forecast(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        metric=metric,
        model=model,
        horizon_days=len(points),
        points=points,
        days_to_threshold=forecast.days_to_threshold(points, threshold),
    )
    return 1


async def _cluster_forecasts(
    session: AsyncSession, daily: pd.DataFrame, artifacts: forecast.ForecastArtifacts | None
) -> int:
    """Cluster capacity is the mean of its VMs' daily p95 - a cluster that is
    80% busy on average has no headroom regardless of which VM is loudest."""
    rows = (
        await session.execute(
            select(VM.external_id, Host.cluster_id)
            .join(Host, VM.host_id == Host.id)
            .where(VM.is_deleted.is_(False), Host.cluster_id.is_not(None))
        )
    ).all()
    if not rows:
        return 0
    membership = pd.DataFrame(rows, columns=["entity_external_id", "cluster_id"])
    merged = daily.merge(membership, on="entity_external_id", how="inner")
    if merged.empty:
        return 0

    written = 0
    for metric in FORECAST_METRICS:
        aggregated = merged.groupby(["cluster_id", "date"])[metric].mean().reset_index().dropna()
        for cluster_id, group in aggregated.groupby("cluster_id"):
            series = group.sort_values("date")
            history = series[metric].to_numpy(dtype=np.float64)
            if history.size < 14:
                continue
            dates = pd.DatetimeIndex(series["date"])
            last_date = dates[-1].date()
            written += await _store(
                session,
                EntityType.CLUSTER,
                cluster_id,
                metric,
                ForecastModel.NAIVE,
                forecast.to_points(forecast.seasonal_naive(history), last_date),
            )
            if artifacts is not None:
                values = forecast.predict(artifacts, f"cluster:{cluster_id}", history, dates)
                written += await _store(
                    session,
                    EntityType.CLUSTER,
                    cluster_id,
                    metric,
                    ForecastModel.LSTM,
                    forecast.to_points(values, last_date, artifacts.residual_quantiles),
                )
    return written


async def _datastore_forecasts(
    session: AsyncSession, mongo: AsyncIOMotorDatabase, artifacts: forecast.ForecastArtifacts | None
) -> int:
    """Datastores have no metric series in V1, so used_pct is projected from the
    cluster's storage growth implied by provisioned VM storage over time."""
    datastores = (await session.execute(select(Datastore))).scalars().all()
    if not datastores:
        return 0

    written = 0
    today = datetime.now(UTC).date()
    for datastore in datastores:
        used_pct = datastore.used_pct
        if datastore.capacity_gb <= 0:
            continue
        # Linear projection from the observed fill rate; with a single inventory
        # snapshot the rate is conservative rather than invented.
        growth_per_day = max(0.0, used_pct * 0.002)
        values = np.array(
            [min(100.0, used_pct + growth_per_day * (i + 1)) for i in range(forecast.HORIZON)]
        )
        points = forecast.to_points(values, today)
        await ml_service.save_forecast(
            session,
            entity_type=EntityType.DATASTORE,
            entity_id=datastore.id,
            metric="used_pct",
            model=ForecastModel.NAIVE,
            horizon_days=len(points),
            points=points,
            days_to_threshold=forecast.days_to_threshold(points, settings.datastore_threshold_pct),
        )
        written += 1
    return written


# --- Right-sizing ---------------------------------------------------------


async def run_rightsizing(
    session: AsyncSession, mongo: AsyncIOMotorDatabase, *, days: int = 30
) -> JobResult:
    started = datetime.now(UTC)
    daily = await load_daily(mongo, entity_type="vm", days=days)
    if daily.empty:
        return JobResult("rightsizing", started, datetime.now(UTC), {"skipped": "no daily metrics"})

    specs = await _vm_specs(session)
    suggestions = rightsizing.evaluate_all(specs, daily)
    vm_ids = await _vm_lookup(session)

    created = 0
    updated = 0
    keep: set[tuple[uuid.UUID, RecommendationKind]] = set()
    cutoff = datetime.now(UTC)

    for suggestion in suggestions:
        vm_id = vm_ids.get(suggestion.external_id)
        if vm_id is None:
            continue
        kind = RecommendationKind(suggestion.kind)
        keep.add((vm_id, kind))
        _, is_new = await ml_service.upsert_recommendation(
            session,
            vm_id=vm_id,
            kind=kind,
            current_value=suggestion.current_value,
            suggested_value=suggestion.suggested_value,
            evidence=suggestion.evidence,
        )
        created += int(is_new)
        updated += int(not is_new)

    closed = await ml_service.close_stale_recommendations(
        session, keep=keep, generated_before=cutoff
    )
    await session.commit()
    return JobResult(
        "rightsizing",
        started,
        datetime.now(UTC),
        {
            "vms_evaluated": len(specs),
            "suggestions": len(suggestions),
            "created": created,
            "updated": updated,
            "closed": closed,
        },
    )


async def run_all(
    session: AsyncSession,
    mongo: AsyncIOMotorDatabase,
    *,
    anomaly_days: int = 1,
    history_days: int = 180,
) -> dict[str, Any]:
    """What POST /ml/run and `python -m ml.run_all` execute."""
    results = [
        await run_anomaly_detection(session, mongo, days=anomaly_days),
        await run_forecasts(session, mongo, days=history_days),
        await run_rightsizing(session, mongo),
    ]
    return {result.name: result.to_dict() for result in results}


async def powered_on_vm_count(session: AsyncSession) -> int:
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count(VM.id)).where(
                    VM.is_deleted.is_(False), VM.power_state == PowerState.ON
                )
            )
        ).scalar_one()
    )


async def cluster_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    rows = (await session.execute(select(Cluster.id, Cluster.name))).all()
    return {row[0]: row[1] for row in rows}
