"""Hypervisor source CRUD. Passwords are Fernet-encrypted at rest and decrypted
only when the collector asks for them over /internal (spec 8.2)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import HypervisorSource
from app.db.models.enums import SourceStatus, SourceType
from app.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.schemas.ingest import InternalSourceOut
from app.schemas.sources import SourceCreate, SourceUpdate
from app.security import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)


async def list_sources(
    session: AsyncSession, *, enabled_only: bool = False
) -> list[HypervisorSource]:
    stmt = select(HypervisorSource).order_by(HypervisorSource.name)
    if enabled_only:
        stmt = stmt.where(HypervisorSource.is_enabled.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_source(session: AsyncSession, source_id: uuid.UUID) -> HypervisorSource:
    source = (
        await session.execute(select(HypervisorSource).where(HypervisorSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise NotFoundError(f"Source {source_id} not found")
    return source


async def create_source(session: AsyncSession, payload: SourceCreate) -> HypervisorSource:
    existing = (
        await session.execute(
            select(HypervisorSource.id).where(HypervisorSource.name == payload.name)
        )
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"Source named {payload.name!r} already exists")
    source = HypervisorSource(
        name=payload.name,
        type=payload.type,
        endpoint=payload.endpoint,
        username=payload.username,
        password_encrypted=encrypt_secret(payload.password),
        verify_ssl=payload.verify_ssl,
        is_enabled=payload.is_enabled,
        last_status=SourceStatus.PENDING,
    )
    session.add(source)
    await session.flush()
    return source


async def update_source(
    session: AsyncSession, source_id: uuid.UUID, payload: SourceUpdate
) -> HypervisorSource:
    source = await get_source(session, source_id)
    data = payload.model_dump(exclude_unset=True)
    if "password" in data:
        password = data.pop("password")
        if password:
            source.password_encrypted = encrypt_secret(password)
    for field, value in data.items():
        setattr(source, field, value)
    await session.flush()
    return source


async def set_status(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    status: SourceStatus,
    error: str | None = None,
    collected_at: datetime | None = None,
) -> HypervisorSource:
    source = await get_source(session, source_id)
    source.last_status = status
    source.last_error = error
    if status is SourceStatus.OK and collected_at is not None:
        source.last_inventory_at = collected_at
    await session.flush()
    return source


def to_internal(source: HypervisorSource) -> InternalSourceOut:
    return InternalSourceOut(
        id=source.id,
        name=source.name,
        type=SourceType(source.type),
        endpoint=source.endpoint,
        username=source.username,
        password=decrypt_secret(source.password_encrypted),
        verify_ssl=source.verify_ssl,
    )


async def trigger_collector_sync(source_id: uuid.UUID) -> str:
    """Fire the collector's /collect/trigger. Never blocks on the actual poll."""
    url = f"{settings.collector_url.rstrip('/')}/collect/trigger"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"source_id": str(source_id)},
                headers={"X-Service-Token": settings.internal_service_token},
            )
            resp.raise_for_status()
            return str(resp.json().get("detail", "sync queued"))
    except httpx.HTTPError as exc:
        log.warning("collector sync failed: %s", exc)
        raise ServiceUnavailableError(f"Collector unreachable at {url}") from exc
