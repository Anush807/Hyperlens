"""Async SQLAlchemy engine/session plumbing."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.config import settings

# Tests run each case in its own event loop, and a pooled asyncpg connection
# cannot be handed to a different loop than the one that opened it.
_pool_options: dict[str, object] = (
    {"poolclass": NullPool} if settings.testing else {"pool_size": 10, "max_overflow": 20}
)

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=False,
    **_pool_options,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """Declarative base carrying the columns every table in 6.2 shares."""


class TimestampedUUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
