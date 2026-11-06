from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from app.db.models.enums import AnomalyStatus, EntityType
from app.deps import DbSession, OperatorUser, ViewerUser
from app.pagination import Page, PageParams, page_params
from app.schemas.ml import AnomalyOut, AnomalyPatch
from app.services import audit, ml_service

router = APIRouter(prefix="/anomalies", tags=["anomalies"])

Params = Annotated[PageParams, Depends(page_params)]


@router.get("", response_model=Page[AnomalyOut])
async def list_anomalies(
    session: DbSession,
    _: ViewerUser,
    params: Params,
    status: AnomalyStatus | None = None,
    entity_type: EntityType | None = None,
    entity_id: uuid.UUID | None = None,
    days: int | None = None,
) -> Page[AnomalyOut]:
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    rows, total = await ml_service.list_anomalies(
        session,
        status=status,
        entity_type=entity_type,
        entity_id=entity_id,
        since=since,
        limit=params.limit,
        offset=params.offset,
    )
    items = await ml_service.hydrate_anomalies(session, rows)
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)


@router.patch("/{anomaly_id}", response_model=AnomalyOut)
async def patch_anomaly(
    session: DbSession, user: OperatorUser, anomaly_id: uuid.UUID, payload: AnomalyPatch
) -> AnomalyOut:
    row = await ml_service.set_anomaly_status(session, anomaly_id, payload.status)
    await audit.record(
        session,
        user_id=user.id,
        action=f"anomaly.{payload.status}",
        entity_type="anomaly",
        entity_id=row.id,
        details={"status": str(payload.status)},
    )
    await session.commit()
    return (await ml_service.hydrate_anomalies(session, [row]))[0]
