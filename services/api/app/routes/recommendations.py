from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.db.models.enums import RecommendationKind, RecommendationStatus
from app.deps import DbSession, OperatorUser, ViewerUser
from app.pagination import PageParams, page_params
from app.schemas.ml import RecommendationOut, RecommendationPage, RecommendationPatch
from app.services import audit, inventory_service, ml_service

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

Params = Annotated[PageParams, Depends(page_params)]


@router.get("", response_model=RecommendationPage)
async def list_recommendations(
    session: DbSession,
    _: ViewerUser,
    params: Params,
    kind: RecommendationKind | None = None,
    status: RecommendationStatus | None = RecommendationStatus.OPEN,
    cluster_id: uuid.UUID | None = None,
) -> RecommendationPage:
    rows, total = await ml_service.list_recommendations(
        session,
        kind=kind,
        status=status,
        cluster_id=cluster_id,
        limit=params.limit,
        offset=params.offset,
    )
    items = await ml_service.hydrate_recommendations(session, rows)
    # Totals cover every open recommendation, not just the current page.
    vcpus, memory = await inventory_service.reclaimable_totals(session, cluster_id=cluster_id)
    return RecommendationPage(
        items=items,
        total=total,
        limit=params.limit,
        offset=params.offset,
        total_reclaimable_vcpus=vcpus,
        total_reclaimable_memory_mb=memory,
    )


@router.patch("/{rec_id}", response_model=RecommendationOut)
async def patch_recommendation(
    session: DbSession, user: OperatorUser, rec_id: uuid.UUID, payload: RecommendationPatch
) -> RecommendationOut:
    row = await ml_service.set_recommendation_status(session, rec_id, payload.status)
    await audit.record(
        session,
        user_id=user.id,
        action=f"recommendation.{payload.status}",
        entity_type="recommendation",
        entity_id=row.id,
    )
    await session.commit()
    return (await ml_service.hydrate_recommendations(session, [row]))[0]
