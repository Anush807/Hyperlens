"""Inventory and metric ingest.

The collector never touches a database; it POSTs normalized batches here and this
module is the only writer for inventory tables (spec 2, "Service boundaries").

Upserts are keyed on (source_id, external_id) so re-ingesting an identical batch
is a no-op apart from `last_seen_at` - which Phase 3's acceptance test relies on.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VM, Cluster, Datastore, Host
from app.db.mongo import EVENTS, METRICS
from app.schemas.ingest import IngestResult, InventoryBatch, MetricsBatch

log = logging.getLogger(__name__)


async def _upsert(
    session: AsyncSession,
    model: Any,
    rows: list[dict[str, Any]],
    constraint: str,
    update_cols: list[str],
) -> dict[str, uuid.UUID]:
    """Bulk ON CONFLICT upsert; returns {external_id: id} for the whole set."""
    if not rows:
        return {}
    insert_stmt = pg_insert(model).values(rows)
    stmt = insert_stmt.on_conflict_do_update(
        constraint=constraint,
        set_={col: getattr(insert_stmt.excluded, col) for col in update_cols}
        | {"updated_at": datetime.now(UTC)},
    ).returning(model.id, model.external_id)
    result = await session.execute(stmt)
    return {row[1]: row[0] for row in result.all()}


async def ingest_inventory(session: AsyncSession, batch: InventoryBatch) -> IngestResult:
    now = datetime.now(UTC)
    collected_at = batch.collected_at
    source_id = batch.source_id

    cluster_ids = await _upsert(
        session,
        Cluster,
        [
            {"source_id": source_id, "external_id": c.external_id, "name": c.name}
            for c in batch.clusters
        ],
        "uq_clusters_source_ext",
        ["name"],
    )

    host_ids = await _upsert(
        session,
        Host,
        [
            {
                "source_id": source_id,
                "cluster_id": cluster_ids.get(h.cluster_external_id or ""),
                "external_id": h.external_id,
                "name": h.name,
                "cpu_cores": h.cpu_cores,
                "cpu_mhz_per_core": h.cpu_mhz_per_core,
                "memory_mb": h.memory_mb,
                "power_state": h.power_state,
                "last_seen_at": collected_at,
            }
            for h in batch.hosts
        ],
        "uq_hosts_source_ext",
        [
            "cluster_id",
            "name",
            "cpu_cores",
            "cpu_mhz_per_core",
            "memory_mb",
            "power_state",
            "last_seen_at",
        ],
    )

    vm_ids = await _upsert(
        session,
        VM,
        [
            {
                "source_id": source_id,
                "host_id": host_ids.get(v.host_external_id or ""),
                "external_id": v.external_id,
                "name": v.name,
                "vcpus": v.vcpus,
                "memory_mb": v.memory_mb,
                "provisioned_storage_gb": v.provisioned_storage_gb,
                "guest_os": v.guest_os,
                "power_state": v.power_state,
                "last_seen_at": collected_at,
                "is_deleted": False,
            }
            for v in batch.vms
        ],
        "uq_vms_source_ext",
        [
            "host_id",
            "name",
            "vcpus",
            "memory_mb",
            "provisioned_storage_gb",
            "guest_os",
            "power_state",
            "last_seen_at",
            "is_deleted",
        ],
    )

    datastore_ids = await _upsert(
        session,
        Datastore,
        [
            {
                "source_id": source_id,
                "external_id": d.external_id,
                "name": d.name,
                "capacity_gb": d.capacity_gb,
                "free_gb": d.free_gb,
            }
            for d in batch.datastores
        ],
        "uq_datastores_source_ext",
        ["name", "capacity_gb", "free_gb"],
    )

    soft_deleted = await _soft_delete_missing_vms(session, source_id, set(vm_ids))

    log.info(
        "ingested inventory batch",
        extra={
            "source_id": str(source_id),
            "clusters": len(cluster_ids),
            "hosts": len(host_ids),
            "vms": len(vm_ids),
            "datastores": len(datastore_ids),
            "soft_deleted_vms": soft_deleted,
            "collected_at": now.isoformat(),
        },
    )
    return IngestResult(
        clusters=len(cluster_ids),
        hosts=len(host_ids),
        vms=len(vm_ids),
        datastores=len(datastore_ids),
        soft_deleted_vms=soft_deleted,
    )


async def _soft_delete_missing_vms(
    session: AsyncSession, source_id: uuid.UUID, seen_external_ids: set[str]
) -> int:
    """A full batch is authoritative: anything absent is gone from the hypervisor.

    An empty batch is treated as "nothing collected", not "everything deleted" -
    a failed poll must not wipe the inventory view.
    """
    if not seen_external_ids:
        return 0
    stmt = (
        update(VM)
        .where(
            VM.source_id == source_id,
            VM.is_deleted.is_(False),
            VM.external_id.notin_(seen_external_ids),
        )
        .values(is_deleted=True, updated_at=datetime.now(UTC))
    )
    return int((await session.execute(stmt)).rowcount or 0)


async def ingest_metrics(mongo: AsyncIOMotorDatabase, batch: MetricsBatch) -> IngestResult:
    """Inserts into the time-series collection. Duplicates are tolerated: the
    rollup aggregates by hour/day, so a replayed batch cannot skew a mean."""
    if not batch.points:
        return IngestResult()
    docs = [
        {
            "ts": p.ts,
            "meta": {
                "source_id": str(batch.source_id),
                "entity_type": p.entity_type,
                "entity_external_id": p.entity_external_id,
            },
            "cpu_pct": round(p.cpu_pct, 3),
            "mem_pct": round(p.mem_pct, 3),
            "disk_iops": round(p.disk_iops, 2),
            "disk_latency_ms": round(p.disk_latency_ms, 3),
            "net_kbps": round(p.net_kbps, 2),
        }
        for p in batch.points
    ]
    await mongo[METRICS].insert_many(docs, ordered=False)
    return IngestResult(points=len(docs))


async def record_event(mongo: AsyncIOMotorDatabase, kind: str, payload: dict[str, Any]) -> None:
    """Collector run logs and ingest errors (30-day TTL)."""
    await mongo[EVENTS].insert_one({"kind": kind, "created_at": datetime.now(UTC), **payload})


async def resolve_external_ids(
    session: AsyncSession, entity_type: str, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Maps internal UUIDs to the hypervisor ids used as the Mongo meta key."""
    model: Any = VM if entity_type == "vm" else Host
    rows = (
        await session.execute(select(model.id, model.external_id).where(model.id.in_(ids)))
    ).all()
    return {row[0]: row[1] for row in rows}


def metric_update_ops(docs: list[dict[str, Any]]) -> list[UpdateOne]:
    """Used by the rollup job - exposed here so tests can assert the shape."""
    return [
        UpdateOne(
            {
                "entity_type": d["entity_type"],
                "entity_external_id": d["entity_external_id"],
                "date": d["date"],
            },
            {"$set": d},
            upsert=True,
        )
        for d in docs
    ]
