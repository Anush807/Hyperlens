"""Forecast reads, including the cluster/datastore capacity view."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from sqlalchemy import func, select

from app.config import settings
from app.db.models import VM, Cluster, Datastore, Host
from app.db.models.enums import EntityType, ForecastModel
from app.deps import DbSession, ViewerUser
from app.errors import NotFoundError
from app.schemas.ml import CapacityOut, EntityForecasts
from app.services import ml_service
from app.services.ml_state import state

router = APIRouter(tags=["forecasts"])

# Values are mapped classes; `Any` because mypy cannot see the shared
# `id`/`name` columns through the declarative base.
_MODEL_FOR: dict[EntityType, Any] = {
    EntityType.VM: VM,
    EntityType.HOST: Host,
    EntityType.CLUSTER: Cluster,
    EntityType.DATASTORE: Datastore,
}


async def _entity_name(session: DbSession, entity_type: EntityType, entity_id: uuid.UUID) -> str:
    model = _MODEL_FOR[entity_type]
    name = (
        await session.execute(select(model.name).where(model.id == entity_id))
    ).scalar_one_or_none()
    if name is None:
        raise NotFoundError(f"{entity_type} {entity_id} not found")
    return name


@router.get("/forecasts/{entity_type}/{entity_id}", response_model=EntityForecasts)
async def get_forecasts(
    session: DbSession,
    _: ViewerUser,
    entity_type: Literal["vm", "host", "cluster", "datastore"],
    entity_id: uuid.UUID,
) -> EntityForecasts:
    et = EntityType(entity_type)
    name = await _entity_name(session, et, entity_id)
    forecasts = await ml_service.latest_forecasts(session, entity_type=et, entity_id=entity_id)
    threshold = (
        settings.datastore_threshold_pct
        if et is EntityType.DATASTORE
        else settings.cpu_threshold_pct
    )
    return EntityForecasts(
        entity_type=et,
        entity_id=entity_id,
        entity_name=name,
        threshold_pct=threshold,
        preferred_model=ForecastModel(state.preferred_forecast_model),
        forecasts=forecasts,
    )


@router.get("/capacity/clusters/{cluster_id}", response_model=CapacityOut)
async def cluster_capacity(session: DbSession, _: ViewerUser, cluster_id: uuid.UUID) -> CapacityOut:
    name = await _entity_name(session, EntityType.CLUSTER, cluster_id)
    forecasts = await ml_service.latest_forecasts(
        session, entity_type=EntityType.CLUSTER, entity_id=cluster_id
    )
    allocations = await _cluster_headroom(session, cluster_id)
    preferred = state.preferred_forecast_model
    return CapacityOut(
        entity_type=EntityType.CLUSTER,
        entity_id=cluster_id,
        entity_name=name,
        preferred_model=ForecastModel(preferred),
        metrics=_days_to_threshold(forecasts, preferred),
        forecasts=forecasts,
        headroom=allocations,
    )


@router.get("/capacity/datastores/{datastore_id}", response_model=CapacityOut)
async def datastore_capacity(
    session: DbSession, _: ViewerUser, datastore_id: uuid.UUID
) -> CapacityOut:
    row = (
        await session.execute(select(Datastore).where(Datastore.id == datastore_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Datastore {datastore_id} not found")
    forecasts = await ml_service.latest_forecasts(
        session, entity_type=EntityType.DATASTORE, entity_id=datastore_id
    )
    preferred = state.preferred_forecast_model
    return CapacityOut(
        entity_type=EntityType.DATASTORE,
        entity_id=datastore_id,
        entity_name=row.name,
        preferred_model=ForecastModel(preferred),
        metrics=_days_to_threshold(forecasts, preferred),
        forecasts=forecasts,
        headroom={
            "capacity_gb": round(row.capacity_gb, 2),
            "free_gb": round(row.free_gb, 2),
            "used_pct": row.used_pct,
        },
    )


def _days_to_threshold(forecasts: list, preferred: str) -> dict[str, int | None]:
    """days_to_threshold per metric, taken from the preferred model where it
    exists and from whatever else is stored when it does not."""
    chosen = {f.metric: f.days_to_threshold for f in forecasts if str(f.model) == preferred}
    for item in forecasts:
        chosen.setdefault(item.metric, item.days_to_threshold)
    return chosen


async def _cluster_headroom(session: DbSession, cluster_id: uuid.UUID) -> dict[str, float]:
    cores, memory = (
        await session.execute(
            select(
                func.coalesce(func.sum(Host.cpu_cores), 0),
                func.coalesce(func.sum(Host.memory_mb), 0),
            ).where(Host.cluster_id == cluster_id)
        )
    ).one()
    vcpus, vm_memory = (
        await session.execute(
            select(
                func.coalesce(func.sum(VM.vcpus), 0),
                func.coalesce(func.sum(VM.memory_mb), 0),
            )
            .join(Host, VM.host_id == Host.id)
            .where(Host.cluster_id == cluster_id, VM.is_deleted.is_(False))
        )
    ).one()
    return {
        "physical_cores": float(cores),
        "allocated_vcpus": float(vcpus),
        "vcpu_overcommit_ratio": round(float(vcpus) / float(cores), 2) if cores else 0.0,
        "physical_memory_mb": float(memory),
        "allocated_memory_mb": float(vm_memory),
        "memory_overcommit_ratio": round(float(vm_memory) / float(memory), 2) if memory else 0.0,
    }
