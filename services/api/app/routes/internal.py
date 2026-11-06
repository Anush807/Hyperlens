"""Service-to-service endpoints. X-Service-Token only, hidden from the public
OpenAPI schema so the generated frontend client cannot reach them."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.db.models.enums import SourceStatus
from app.deps import DbSession, MongoDb, require_service_token
from app.schemas.ingest import (
    IngestResult,
    InternalSourceOut,
    InventoryBatch,
    MetricsBatch,
    SourceStatusIn,
)
from app.services import ingest_service, source_service

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(require_service_token)],
)


@router.get("/sources", response_model=list[InternalSourceOut])
async def internal_sources(session: DbSession) -> list[InternalSourceOut]:
    sources = await source_service.list_sources(session, enabled_only=True)
    return [source_service.to_internal(s) for s in sources]


@router.post("/inventory", response_model=IngestResult)
async def ingest_inventory(session: DbSession, batch: InventoryBatch) -> IngestResult:
    # Checked up front so a stale source id surfaces as a 404 the collector can
    # log, rather than a foreign-key violation surfacing as a 500.
    await source_service.get_source(session, batch.source_id)
    result = await ingest_service.ingest_inventory(session, batch)
    await source_service.set_status(
        session,
        batch.source_id,
        status=SourceStatus.OK,
        error=None,
        collected_at=batch.collected_at,
    )
    await session.commit()
    return result


@router.post("/metrics", response_model=IngestResult)
async def ingest_metrics(session: DbSession, mongo: MongoDb, batch: MetricsBatch) -> IngestResult:
    await source_service.get_source(session, batch.source_id)
    return await ingest_service.ingest_metrics(mongo, batch)


@router.post("/sources/{source_id}/status", response_model=dict)
async def report_status(
    session: DbSession, mongo: MongoDb, source_id: uuid.UUID, payload: SourceStatusIn
) -> dict[str, str]:
    await source_service.set_status(
        session,
        source_id,
        status=SourceStatus(payload.status),
        error=payload.error,
        collected_at=payload.collected_at,
    )
    await session.commit()
    await ingest_service.record_event(
        mongo,
        "collector_run",
        {"source_id": str(source_id), "status": payload.status, "error": payload.error},
    )
    return {"status": payload.status}
