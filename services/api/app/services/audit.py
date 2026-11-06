"""Audit trail. Every mutating endpoint calls record()."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def record(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Adds the row to the caller's transaction - it commits or rolls back together."""
    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=details or {},
        )
    )
