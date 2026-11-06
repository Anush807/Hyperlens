"""Persistence and read models for ML output (anomalies, forecasts, recommendations).

The learning itself lives in ml/; this module is the boundary between those
batch jobs and the relational store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VM, Anomaly, Cluster, Datastore, Forecast, Host, Recommendation
from app.db.models.enums import (
    AnomalyStatus,
    EntityType,
    ForecastModel,
    RecommendationKind,
    RecommendationStatus,
)
from app.errors import NotFoundError
from app.schemas.ml import AnomalyOut, ForecastOut, RecommendationOut


async def _total(session: AsyncSession, stmt: Select) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )


# --- Anomalies ------------------------------------------------------------


async def list_anomalies(
    session: AsyncSession,
    *,
    status: AnomalyStatus | None = None,
    entity_id: uuid.UUID | None = None,
    entity_type: EntityType | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Anomaly], int]:
    stmt = select(Anomaly)
    if status:
        stmt = stmt.where(Anomaly.status == status)
    if entity_id:
        stmt = stmt.where(Anomaly.entity_id == entity_id)
    if entity_type:
        stmt = stmt.where(Anomaly.entity_type == entity_type)
    if since:
        stmt = stmt.where(Anomaly.detected_at >= since)
    total = await _total(session, stmt)
    rows = (
        (
            await session.execute(
                stmt.order_by(Anomaly.detected_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def hydrate_anomalies(session: AsyncSession, rows: list[Anomaly]) -> list[AnomalyOut]:
    """Attach entity names so the UI and the assistant never show bare UUIDs."""
    names = await _entity_names(session, [(r.entity_type, r.entity_id) for r in rows])
    out = []
    for row in rows:
        item = AnomalyOut.model_validate(row)
        item.entity_name = names.get((row.entity_type, row.entity_id))
        out.append(item)
    return out


async def _entity_names(
    session: AsyncSession, keys: list[tuple[EntityType, uuid.UUID]]
) -> dict[tuple[EntityType, uuid.UUID], str]:
    by_type: dict[EntityType, set[uuid.UUID]] = {}
    for entity_type, entity_id in keys:
        by_type.setdefault(EntityType(entity_type), set()).add(entity_id)
    # `Any` because mypy cannot see the shared id/name columns through the
    # declarative base.
    model_for: dict[EntityType, Any] = {
        EntityType.VM: VM,
        EntityType.HOST: Host,
        EntityType.CLUSTER: Cluster,
        EntityType.DATASTORE: Datastore,
    }
    names: dict[tuple[EntityType, uuid.UUID], str] = {}
    for entity_type, ids in by_type.items():
        model = model_for[entity_type]
        rows = (await session.execute(select(model.id, model.name).where(model.id.in_(ids)))).all()
        for row_id, name in rows:
            names[(entity_type, row_id)] = name
    return names


async def get_anomaly(session: AsyncSession, anomaly_id: uuid.UUID) -> Anomaly:
    row = (
        await session.execute(select(Anomaly).where(Anomaly.id == anomaly_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Anomaly {anomaly_id} not found")
    return row


async def set_anomaly_status(
    session: AsyncSession, anomaly_id: uuid.UUID, status: AnomalyStatus
) -> Anomaly:
    row = await get_anomaly(session, anomaly_id)
    row.status = status
    await session.flush()
    return row


async def upsert_anomaly(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    score: float,
    features: dict[str, Any],
) -> tuple[Anomaly, bool]:
    """Overlapping windows for the same entity extend the existing row rather
    than creating a second alert for one incident (spec 9.2, "merge")."""
    existing = (
        await session.execute(
            select(Anomaly)
            .where(
                Anomaly.entity_type == entity_type,
                Anomaly.entity_id == entity_id,
                Anomaly.window_end >= window_start - timedelta(minutes=1),
                Anomaly.window_start <= window_end + timedelta(minutes=1),
            )
            .order_by(Anomaly.window_start)
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.window_start = min(existing.window_start, window_start)
        existing.window_end = max(existing.window_end, window_end)
        existing.score = min(existing.score, score)  # lower IsolationForest score = more anomalous
        existing.features = features
        await session.flush()
        return existing, False

    row = Anomaly(
        entity_type=entity_type,
        entity_id=entity_id,
        detected_at=datetime.now(UTC),
        window_start=window_start,
        window_end=window_end,
        score=score,
        features=features,
        status=AnomalyStatus.OPEN,
    )
    session.add(row)
    await session.flush()
    return row, True


# --- Forecasts ------------------------------------------------------------


async def save_forecast(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity_id: uuid.UUID,
    metric: str,
    model: ForecastModel,
    horizon_days: int,
    points: list[dict[str, Any]],
    days_to_threshold: int | None,
) -> Forecast:
    row = Forecast(
        entity_type=entity_type,
        entity_id=entity_id,
        metric=metric,
        model=model,
        generated_at=datetime.now(UTC),
        horizon_days=horizon_days,
        points=points,
        days_to_threshold=days_to_threshold,
    )
    session.add(row)
    await session.flush()
    return row


async def latest_forecasts(
    session: AsyncSession, *, entity_type: EntityType, entity_id: uuid.UUID
) -> list[ForecastOut]:
    """One row per (metric, model): the most recently generated."""
    newest = (
        select(
            Forecast.metric,
            Forecast.model,
            func.max(Forecast.generated_at).label("generated_at"),
        )
        .where(Forecast.entity_type == entity_type, Forecast.entity_id == entity_id)
        .group_by(Forecast.metric, Forecast.model)
        .subquery()
    )
    rows = (
        (
            await session.execute(
                select(Forecast)
                .join(
                    newest,
                    (Forecast.metric == newest.c.metric)
                    & (Forecast.model == newest.c.model)
                    & (Forecast.generated_at == newest.c.generated_at),
                )
                .where(Forecast.entity_type == entity_type, Forecast.entity_id == entity_id)
                .order_by(Forecast.metric, Forecast.model)
            )
        )
        .scalars()
        .all()
    )
    return [ForecastOut.model_validate(r) for r in rows]


# --- Recommendations ------------------------------------------------------


async def list_recommendations(
    session: AsyncSession,
    *,
    kind: RecommendationKind | None = None,
    status: RecommendationStatus | None = None,
    vm_id: uuid.UUID | None = None,
    cluster_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Recommendation], int]:
    stmt = (
        select(Recommendation)
        .join(VM, Recommendation.vm_id == VM.id)
        .where(VM.is_deleted.is_(False))
    )
    if kind:
        stmt = stmt.where(Recommendation.kind == kind)
    if status:
        stmt = stmt.where(Recommendation.status == status)
    if vm_id:
        stmt = stmt.where(Recommendation.vm_id == vm_id)
    if cluster_id:
        stmt = stmt.join(Host, VM.host_id == Host.id).where(Host.cluster_id == cluster_id)
    total = await _total(session, stmt)
    rows = (
        (
            await session.execute(
                stmt.order_by(Recommendation.generated_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def hydrate_recommendations(
    session: AsyncSession, rows: list[Recommendation]
) -> list[RecommendationOut]:
    if not rows:
        return []
    vm_rows = (
        await session.execute(
            select(VM.id, VM.name, VM.vcpus, VM.memory_mb, Cluster.name)
            .outerjoin(Host, VM.host_id == Host.id)
            .outerjoin(Cluster, Host.cluster_id == Cluster.id)
            .where(VM.id.in_([r.vm_id for r in rows]))
        )
    ).all()
    vms = {r[0]: r for r in vm_rows}

    out: list[RecommendationOut] = []
    for row in rows:
        item = RecommendationOut.model_validate(row)
        vm = vms.get(row.vm_id)
        if vm:
            item.vm_name = vm[1]
            item.cluster_name = vm[4]
            item.reclaimable_vcpus, item.reclaimable_memory_mb = _reclaimable(row, vm[2], vm[3])
        out.append(item)
    return out


def _reclaimable(rec: Recommendation, vcpus: int, memory_mb: int) -> tuple[int, int]:
    if rec.kind == RecommendationKind.IDLE:
        return vcpus, memory_mb
    if rec.kind == RecommendationKind.DOWNSIZE_CPU:
        return max(0, int(rec.current_value - rec.suggested_value)), 0
    if rec.kind == RecommendationKind.DOWNSIZE_MEM:
        return 0, max(0, int(rec.current_value - rec.suggested_value))
    return 0, 0


async def get_recommendation(session: AsyncSession, rec_id: uuid.UUID) -> Recommendation:
    row = (
        await session.execute(select(Recommendation).where(Recommendation.id == rec_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Recommendation {rec_id} not found")
    return row


async def set_recommendation_status(
    session: AsyncSession, rec_id: uuid.UUID, status: RecommendationStatus
) -> Recommendation:
    row = await get_recommendation(session, rec_id)
    row.status = status
    await session.flush()
    return row


async def upsert_recommendation(
    session: AsyncSession,
    *,
    vm_id: uuid.UUID,
    kind: RecommendationKind,
    current_value: float,
    suggested_value: float,
    evidence: dict[str, Any],
) -> tuple[Recommendation, bool]:
    """Re-running the job updates the open row for (vm, kind) instead of
    stacking duplicates (spec 9.4). Dismissed rows stay dismissed."""
    existing = (
        await session.execute(
            select(Recommendation).where(
                Recommendation.vm_id == vm_id,
                Recommendation.kind == kind,
                Recommendation.status == RecommendationStatus.OPEN,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if existing is not None:
        existing.current_value = current_value
        existing.suggested_value = suggested_value
        existing.evidence = evidence
        existing.generated_at = now
        await session.flush()
        return existing, False

    row = Recommendation(
        vm_id=vm_id,
        kind=kind,
        current_value=current_value,
        suggested_value=suggested_value,
        evidence=evidence,
        generated_at=now,
        status=RecommendationStatus.OPEN,
    )
    session.add(row)
    await session.flush()
    return row, True


async def close_stale_recommendations(
    session: AsyncSession,
    *,
    keep: set[tuple[uuid.UUID, RecommendationKind]],
    generated_before: datetime,
) -> int:
    """A VM that no longer matches a rule should stop being recommended."""
    rows = (
        (
            await session.execute(
                select(Recommendation).where(
                    Recommendation.status == RecommendationStatus.OPEN,
                    Recommendation.generated_at < generated_before,
                )
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for row in rows:
        if (row.vm_id, RecommendationKind(row.kind)) not in keep:
            row.status = RecommendationStatus.DISMISSED
            closed += 1
    await session.flush()
    return closed
