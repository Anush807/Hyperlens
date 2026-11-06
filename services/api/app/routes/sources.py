from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.deps import AdminUser, DbSession, OperatorUser, ViewerUser
from app.schemas.sources import SourceCreate, SourceOut, SourceUpdate, SyncResult
from app.services import audit, source_service

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceOut])
async def list_sources(session: DbSession, _: ViewerUser) -> list[SourceOut]:
    sources = await source_service.list_sources(session)
    return [SourceOut.model_validate(s) for s in sources]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(session: DbSession, admin: AdminUser, payload: SourceCreate) -> SourceOut:
    source = await source_service.create_source(session, payload)
    await audit.record(
        session,
        user_id=admin.id,
        action="source.create",
        entity_type="hypervisor_source",
        entity_id=source.id,
        details={"name": source.name, "type": str(source.type)},
    )
    await session.commit()
    return SourceOut.model_validate(source)


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    session: DbSession, admin: AdminUser, source_id: uuid.UUID, payload: SourceUpdate
) -> SourceOut:
    source = await source_service.update_source(session, source_id, payload)
    await audit.record(
        session,
        user_id=admin.id,
        action="source.update",
        entity_type="hypervisor_source",
        entity_id=source.id,
        # Never log the password, even redacted-by-convention.
        details=payload.model_dump(exclude_unset=True, exclude={"password"}),
    )
    await session.commit()
    return SourceOut.model_validate(source)


@router.post("/{source_id}/sync", response_model=SyncResult)
async def sync_source(session: DbSession, user: OperatorUser, source_id: uuid.UUID) -> SyncResult:
    source = await source_service.get_source(session, source_id)
    detail = await source_service.trigger_collector_sync(source.id)
    await audit.record(
        session,
        user_id=user.id,
        action="source.sync",
        entity_type="hypervisor_source",
        entity_id=source.id,
    )
    await session.commit()
    return SyncResult(triggered=True, detail=detail)
