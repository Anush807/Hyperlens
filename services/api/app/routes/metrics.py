from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.db.models import VM, Host
from app.deps import DbSession, MongoDb, ViewerUser
from app.errors import NotFoundError
from app.schemas.metrics import MetricSeries, Resolution
from app.services import metrics_service

router = APIRouter(prefix="/metrics", tags=["metrics"])

# Raw resolution over a long window would return hundreds of thousands of points.
MAX_RAW_DAYS = 7


async def _resolve_entity(
    session: DbSession, entity_type: str, entity_id: uuid.UUID
) -> tuple[str, str]:
    model = VM if entity_type == "vm" else Host
    row = (
        await session.execute(select(model.external_id, model.name).where(model.id == entity_id))
    ).first()
    if row is None:
        raise NotFoundError(f"{entity_type} {entity_id} not found")
    return row[0], row[1]


@router.get("/{entity_type}/{entity_id}", response_model=MetricSeries)
async def get_metrics(
    session: DbSession,
    mongo: MongoDb,
    _: ViewerUser,
    entity_type: Literal["vm", "host"],
    entity_id: uuid.UUID,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    resolution: Resolution = "hourly",
) -> MetricSeries:
    external_id, name = await _resolve_entity(session, entity_type, entity_id)
    to_ts = to or datetime.now(UTC)
    from_ts = from_ or (to_ts - timedelta(days=7))
    if from_ts >= to_ts:
        from_ts = to_ts - timedelta(days=1)
    if resolution == "raw" and (to_ts - from_ts) > timedelta(days=MAX_RAW_DAYS):
        from_ts = to_ts - timedelta(days=MAX_RAW_DAYS)

    points = await metrics_service.get_series(
        mongo,
        entity_type=entity_type,
        external_id=external_id,
        from_ts=from_ts,
        to_ts=to_ts,
        resolution=resolution,
    )
    return MetricSeries(
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_name=name,
        resolution=resolution,
        from_ts=from_ts,
        to_ts=to_ts,
        points=points,
    )
