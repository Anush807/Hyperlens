"""Read-side queries over the inventory tables, plus the dashboard summary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VM, Anomaly, Cluster, Datastore, Host, HypervisorSource, Recommendation
from app.db.models.enums import (
    AnomalyStatus,
    PowerState,
    RecommendationKind,
    RecommendationStatus,
)
from app.db.mongo import METRICS
from app.errors import NotFoundError
from app.schemas.inventory import (
    ClusterAllocation,
    ClusterOut,
    DatastoreOut,
    HostOut,
    InventorySummary,
    LatestMetrics,
    VMOut,
)


async def _count(session: AsyncSession, stmt: Select) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )


async def list_clusters(
    session: AsyncSession, *, source_id: uuid.UUID | None, q: str | None, limit: int, offset: int
) -> tuple[list[ClusterOut], int]:
    stmt = select(Cluster)
    if source_id:
        stmt = stmt.where(Cluster.source_id == source_id)
    if q:
        stmt = stmt.where(Cluster.name.ilike(f"%{q}%"))
    total = await _count(session, stmt)
    rows = (
        (await session.execute(stmt.order_by(Cluster.name).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    if not rows:
        return [], total

    ids = [c.id for c in rows]
    host_counts: dict[uuid.UUID, int] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Host.cluster_id, func.count(Host.id))
                .where(Host.cluster_id.in_(ids))
                .group_by(Host.cluster_id)
            )
        ).all()
    }
    vm_counts: dict[uuid.UUID, int] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Host.cluster_id, func.count(VM.id))
                .join(VM, VM.host_id == Host.id)
                .where(Host.cluster_id.in_(ids), VM.is_deleted.is_(False))
                .group_by(Host.cluster_id)
            )
        ).all()
    }
    items = [
        ClusterOut(
            id=c.id,
            source_id=c.source_id,
            external_id=c.external_id,
            name=c.name,
            host_count=host_counts.get(c.id, 0),
            vm_count=vm_counts.get(c.id, 0),
        )
        for c in rows
    ]
    return items, total


async def list_hosts(
    session: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    cluster_id: uuid.UUID | None,
    power_state: PowerState | None,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[list[HostOut], int]:
    stmt = select(Host, Cluster.name).outerjoin(Cluster, Host.cluster_id == Cluster.id)
    if source_id:
        stmt = stmt.where(Host.source_id == source_id)
    if cluster_id:
        stmt = stmt.where(Host.cluster_id == cluster_id)
    if power_state:
        stmt = stmt.where(Host.power_state == power_state)
    if q:
        stmt = stmt.where(Host.name.ilike(f"%{q}%"))
    total = await _count(session, stmt)
    rows = (await session.execute(stmt.order_by(Host.name).limit(limit).offset(offset))).all()
    if not rows:
        return [], total

    host_ids = [h.id for h, _ in rows]
    vm_counts: dict[uuid.UUID, int] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(VM.host_id, func.count(VM.id))
                .where(VM.host_id.in_(host_ids), VM.is_deleted.is_(False))
                .group_by(VM.host_id)
            )
        ).all()
    }
    items = []
    for host, cluster_name in rows:
        out = HostOut.model_validate(host)
        out.cluster_name = cluster_name
        out.vm_count = vm_counts.get(host.id, 0)
        items.append(out)
    return items, total


def _vm_query() -> Select:
    return (
        select(VM, Host.name, Cluster.id, Cluster.name, HypervisorSource.type)
        .outerjoin(Host, VM.host_id == Host.id)
        .outerjoin(Cluster, Host.cluster_id == Cluster.id)
        .outerjoin(HypervisorSource, VM.source_id == HypervisorSource.id)
    )


def _to_vm_out(row: Any) -> VMOut:
    vm, host_name, cluster_id, cluster_name, source_type = row
    out = VMOut.model_validate(vm)
    out.host_name = host_name
    out.cluster_id = cluster_id
    out.cluster_name = cluster_name
    out.source_type = source_type
    return out


async def list_vms(
    session: AsyncSession,
    *,
    source_id: uuid.UUID | None,
    cluster_id: uuid.UUID | None,
    host_id: uuid.UUID | None,
    power_state: PowerState | None,
    q: str | None,
    include_deleted: bool,
    min_vcpus: int | None = None,
    limit: int,
    offset: int,
) -> tuple[list[VMOut], int]:
    stmt = _vm_query()
    if not include_deleted:
        stmt = stmt.where(VM.is_deleted.is_(False))
    if source_id:
        stmt = stmt.where(VM.source_id == source_id)
    if cluster_id:
        stmt = stmt.where(Cluster.id == cluster_id)
    if host_id:
        stmt = stmt.where(VM.host_id == host_id)
    if power_state:
        stmt = stmt.where(VM.power_state == power_state)
    if min_vcpus:
        stmt = stmt.where(VM.vcpus >= min_vcpus)
    if q:
        stmt = stmt.where(or_(VM.name.ilike(f"%{q}%"), VM.guest_os.ilike(f"%{q}%")))
    total = await _count(session, stmt)
    rows = (await session.execute(stmt.order_by(VM.name).limit(limit).offset(offset))).all()
    return [_to_vm_out(r) for r in rows], total


async def get_vm(session: AsyncSession, vm_id: uuid.UUID) -> VMOut:
    row = (await session.execute(_vm_query().where(VM.id == vm_id))).first()
    if row is None:
        raise NotFoundError(f"VM {vm_id} not found")
    return _to_vm_out(row)


async def find_vm_by_name(session: AsyncSession, name: str) -> VMOut | None:
    row = (
        await session.execute(
            _vm_query().where(func.lower(VM.name) == name.lower(), VM.is_deleted.is_(False))
        )
    ).first()
    if row is None:
        row = (
            await session.execute(
                _vm_query()
                .where(VM.name.ilike(f"%{name}%"), VM.is_deleted.is_(False))
                .order_by(VM.name)
                .limit(1)
            )
        ).first()
    return _to_vm_out(row) if row else None


async def list_datastores(
    session: AsyncSession, *, source_id: uuid.UUID | None, q: str | None, limit: int, offset: int
) -> tuple[list[DatastoreOut], int]:
    stmt = select(Datastore)
    if source_id:
        stmt = stmt.where(Datastore.source_id == source_id)
    if q:
        stmt = stmt.where(Datastore.name.ilike(f"%{q}%"))
    total = await _count(session, stmt)
    rows = (
        (await session.execute(stmt.order_by(Datastore.name).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return [
        DatastoreOut(
            id=d.id,
            source_id=d.source_id,
            external_id=d.external_id,
            name=d.name,
            capacity_gb=d.capacity_gb,
            free_gb=d.free_gb,
            used_pct=d.used_pct,
        )
        for d in rows
    ], total


async def latest_metrics(
    mongo: AsyncIOMotorDatabase, entity_type: str, external_id: str
) -> LatestMetrics:
    doc = await mongo[METRICS].find_one(
        {"meta.entity_type": entity_type, "meta.entity_external_id": external_id},
        sort=[("ts", -1)],
    )
    if not doc:
        return LatestMetrics()
    return LatestMetrics(
        ts=doc["ts"],
        cpu_pct=doc.get("cpu_pct"),
        mem_pct=doc.get("mem_pct"),
        disk_iops=doc.get("disk_iops"),
        disk_latency_ms=doc.get("disk_latency_ms"),
        net_kbps=doc.get("net_kbps"),
    )


# --- Allocation / summary -------------------------------------------------


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 2) if denominator else 0.0


async def cluster_allocation(session: AsyncSession) -> list[ClusterAllocation]:
    """vCPU and memory overcommit per cluster, plus an "unassigned" bucket for
    hosts that belong to no cluster (standalone ESXi, and Nutanix in V1)."""
    rows = (
        await session.execute(
            select(
                Cluster.id,
                Cluster.name,
                HypervisorSource.type,
                func.count(func.distinct(Host.id)).label("host_count"),
                func.coalesce(func.sum(Host.cpu_cores), 0).label("cores"),
                func.coalesce(func.sum(Host.memory_mb), 0).label("memory"),
            )
            .select_from(Host)
            .join(HypervisorSource, Host.source_id == HypervisorSource.id)
            .outerjoin(Cluster, Host.cluster_id == Cluster.id)
            .group_by(Cluster.id, Cluster.name, HypervisorSource.type)
        )
    ).all()

    vm_rows = (
        await session.execute(
            select(
                Cluster.id,
                func.count(VM.id),
                func.coalesce(func.sum(VM.vcpus), 0),
                func.coalesce(func.sum(VM.memory_mb), 0),
            )
            .select_from(VM)
            .join(Host, VM.host_id == Host.id)
            .outerjoin(Cluster, Host.cluster_id == Cluster.id)
            .where(VM.is_deleted.is_(False))
            .group_by(Cluster.id)
        )
    ).all()
    vm_by_cluster = {cid: (count, vcpus, mem) for cid, count, vcpus, mem in vm_rows}

    out: list[ClusterAllocation] = []
    for cluster_id, cluster_name, source_type, host_count, cores, memory in rows:
        vm_count, vcpus, vm_mem = vm_by_cluster.get(cluster_id, (0, 0, 0))
        out.append(
            ClusterAllocation(
                cluster_id=cluster_id,
                cluster_name=cluster_name or "(unassigned)",
                source_type=source_type,
                host_count=int(host_count),
                vm_count=int(vm_count),
                physical_cores=int(cores),
                allocated_vcpus=int(vcpus),
                vcpu_overcommit_ratio=_ratio(float(vcpus), float(cores)),
                physical_memory_mb=int(memory),
                allocated_memory_mb=int(vm_mem),
                memory_overcommit_ratio=_ratio(float(vm_mem), float(memory)),
            )
        )
    return sorted(out, key=lambda c: c.cluster_name)


_RECLAIM_KINDS = (RecommendationKind.DOWNSIZE_CPU, RecommendationKind.IDLE)


async def summary(session: AsyncSession) -> InventorySummary:
    sources = (await session.execute(select(func.count(HypervisorSource.id)))).scalar_one()
    clusters = (await session.execute(select(func.count(Cluster.id)))).scalar_one()
    hosts = (await session.execute(select(func.count(Host.id)))).scalar_one()
    vm_stats = (
        await session.execute(
            select(
                func.count(VM.id),
                func.coalesce(func.sum(VM.vcpus), 0),
                func.coalesce(func.sum(VM.memory_mb), 0),
            ).where(VM.is_deleted.is_(False))
        )
    ).one()
    powered_on = (
        await session.execute(
            select(func.count(VM.id)).where(
                VM.is_deleted.is_(False), VM.power_state == PowerState.ON
            )
        )
    ).scalar_one()
    host_stats = (
        await session.execute(
            select(
                func.coalesce(func.sum(Host.cpu_cores), 0),
                func.coalesce(func.sum(Host.memory_mb), 0),
            )
        )
    ).one()
    ds_stats = (
        await session.execute(
            select(
                func.count(Datastore.id),
                func.coalesce(func.sum(Datastore.capacity_gb), 0.0),
                func.coalesce(func.sum(Datastore.free_gb), 0.0),
            )
        )
    ).one()
    open_anomalies = (
        await session.execute(
            select(func.count(Anomaly.id)).where(Anomaly.status == AnomalyStatus.OPEN)
        )
    ).scalar_one()
    open_recs = (
        await session.execute(
            select(func.count(Recommendation.id)).where(
                Recommendation.status == RecommendationStatus.OPEN
            )
        )
    ).scalar_one()

    reclaim_cpu, reclaim_mem = await reclaimable_totals(session)

    return InventorySummary(
        sources=int(sources),
        clusters=int(clusters),
        hosts=int(hosts),
        vms=int(vm_stats[0]),
        vms_powered_on=int(powered_on),
        datastores=int(ds_stats[0]),
        total_vcpus=int(vm_stats[1]),
        total_vm_memory_mb=int(vm_stats[2]),
        total_physical_cores=int(host_stats[0]),
        total_physical_memory_mb=int(host_stats[1]),
        total_storage_capacity_gb=round(float(ds_stats[1]), 2),
        total_storage_free_gb=round(float(ds_stats[2]), 2),
        open_anomalies=int(open_anomalies),
        open_recommendations=int(open_recs),
        reclaimable_vcpus=reclaim_cpu,
        reclaimable_memory_mb=reclaim_mem,
        per_cluster=await cluster_allocation(session),
    )


async def reclaimable_totals(
    session: AsyncSession, *, cluster_id: uuid.UUID | None = None
) -> tuple[int, int]:
    """Sum of what the open recommendations would free.

    `idle` reclaims the VM's whole allocation; the downsize kinds reclaim the
    difference between current and suggested.
    """
    stmt = (
        select(Recommendation, VM)
        .join(VM, Recommendation.vm_id == VM.id)
        .where(Recommendation.status == RecommendationStatus.OPEN, VM.is_deleted.is_(False))
    )
    if cluster_id:
        stmt = stmt.join(Host, VM.host_id == Host.id).where(Host.cluster_id == cluster_id)

    vcpus = 0
    memory = 0
    for rec, vm in (await session.execute(stmt)).all():
        if rec.kind == RecommendationKind.IDLE:
            vcpus += vm.vcpus
            memory += vm.memory_mb
        elif rec.kind == RecommendationKind.DOWNSIZE_CPU:
            vcpus += max(0, int(rec.current_value - rec.suggested_value))
        elif rec.kind == RecommendationKind.DOWNSIZE_MEM:
            memory += max(0, int(rec.current_value - rec.suggested_value))
    return vcpus, memory


async def recent_anomaly_count(session: AsyncSession, days: int = 7) -> int:
    since = datetime.now(UTC) - timedelta(days=days)
    return int(
        (
            await session.execute(
                select(func.count(Anomaly.id)).where(Anomaly.detected_at >= since)
            )
        ).scalar_one()
    )
