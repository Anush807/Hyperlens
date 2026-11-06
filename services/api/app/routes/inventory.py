"""Inventory read endpoints. All list endpoints share the pagination envelope."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.cache import cached
from app.db.models.enums import AnomalyStatus, PowerState, RecommendationStatus
from app.deps import DbSession, MongoDb, ViewerUser
from app.pagination import Page, PageParams, page_params
from app.schemas.inventory import (
    ClusterOut,
    DatastoreOut,
    HostOut,
    InventorySummary,
    LatestMetrics,
    VMDetail,
    VMOut,
)
from app.services import inventory_service, ml_service

router = APIRouter(tags=["inventory"])

Params = Annotated[PageParams, Depends(page_params)]
SUMMARY_TTL_SECONDS = 60.0


@router.get("/inventory/summary", response_model=InventorySummary)
async def inventory_summary(session: DbSession, _: ViewerUser) -> InventorySummary:
    # Cached because the dashboard polls it and it touches every inventory table.
    return await cached(
        "inventory:summary", SUMMARY_TTL_SECONDS, lambda: inventory_service.summary(session)
    )


@router.get("/clusters", response_model=Page[ClusterOut])
async def list_clusters(
    session: DbSession,
    _: ViewerUser,
    params: Params,
    source_id: uuid.UUID | None = None,
    q: str | None = None,
) -> Page[ClusterOut]:
    items, total = await inventory_service.list_clusters(
        session, source_id=source_id, q=q, limit=params.limit, offset=params.offset
    )
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)


@router.get("/hosts", response_model=Page[HostOut])
async def list_hosts(
    session: DbSession,
    _: ViewerUser,
    params: Params,
    source_id: uuid.UUID | None = None,
    cluster_id: uuid.UUID | None = None,
    power_state: PowerState | None = None,
    q: str | None = None,
) -> Page[HostOut]:
    items, total = await inventory_service.list_hosts(
        session,
        source_id=source_id,
        cluster_id=cluster_id,
        power_state=power_state,
        q=q,
        limit=params.limit,
        offset=params.offset,
    )
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)


@router.get("/vms", response_model=Page[VMOut])
async def list_vms(
    session: DbSession,
    _: ViewerUser,
    params: Params,
    source_id: uuid.UUID | None = None,
    cluster_id: uuid.UUID | None = None,
    host_id: uuid.UUID | None = None,
    power_state: PowerState | None = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
    include_deleted: bool = False,
) -> Page[VMOut]:
    items, total = await inventory_service.list_vms(
        session,
        source_id=source_id,
        cluster_id=cluster_id,
        host_id=host_id,
        power_state=power_state,
        q=q,
        include_deleted=include_deleted,
        limit=params.limit,
        offset=params.offset,
    )
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)


@router.get("/vms/{vm_id}", response_model=VMDetail)
async def get_vm(session: DbSession, mongo: MongoDb, _: ViewerUser, vm_id: uuid.UUID) -> VMDetail:
    """VM detail: specs plus the three things an operator needs next to them."""
    vm = await inventory_service.get_vm(session, vm_id)
    metrics: LatestMetrics = await inventory_service.latest_metrics(mongo, "vm", vm.external_id)
    anomalies, _total_a = await ml_service.list_anomalies(
        session, entity_id=vm.id, status=AnomalyStatus.OPEN, limit=20, offset=0
    )
    recs, _total_r = await ml_service.list_recommendations(
        session, vm_id=vm.id, status=RecommendationStatus.OPEN, limit=20, offset=0
    )
    return VMDetail(
        vm=vm,
        latest_metrics=metrics,
        open_anomalies=await ml_service.hydrate_anomalies(session, anomalies),
        open_recommendations=await ml_service.hydrate_recommendations(session, recs),
    )


@router.get("/datastores", response_model=Page[DatastoreOut])
async def list_datastores(
    session: DbSession,
    _: ViewerUser,
    params: Params,
    source_id: uuid.UUID | None = None,
    q: str | None = None,
) -> Page[DatastoreOut]:
    items, total = await inventory_service.list_datastores(
        session, source_id=source_id, q=q, limit=params.limit, offset=params.offset
    )
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)
