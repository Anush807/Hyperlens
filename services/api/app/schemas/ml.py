from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import (
    AnomalyStatus,
    EntityType,
    ForecastModel,
    RecommendationKind,
    RecommendationStatus,
)


class AnomalyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: EntityType
    entity_id: uuid.UUID
    entity_name: str | None = None
    detected_at: datetime
    window_start: datetime
    window_end: datetime
    score: float
    features: dict[str, Any]
    status: AnomalyStatus
    summary: str | None


class AnomalyPatch(BaseModel):
    status: AnomalyStatus


class ForecastPoint(BaseModel):
    date: date
    value: float
    lower: float
    upper: float


class ForecastOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: EntityType
    entity_id: uuid.UUID
    metric: str
    model: ForecastModel
    generated_at: datetime
    horizon_days: int
    points: list[ForecastPoint]
    days_to_threshold: int | None


class EntityForecasts(BaseModel):
    entity_type: EntityType
    entity_id: uuid.UUID
    entity_name: str
    threshold_pct: float
    # Which model the UI should lead with, from the measured holdout (spec 14).
    preferred_model: ForecastModel
    forecasts: list[ForecastOut]


class CapacityOut(BaseModel):
    entity_type: EntityType
    entity_id: uuid.UUID
    entity_name: str
    preferred_model: ForecastModel
    metrics: dict[str, int | None]
    forecasts: list[ForecastOut]
    headroom: dict[str, float]


class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vm_id: uuid.UUID
    vm_name: str | None = None
    cluster_name: str | None = None
    kind: RecommendationKind
    current_value: float
    suggested_value: float
    evidence: dict[str, Any]
    generated_at: datetime
    status: RecommendationStatus
    reclaimable_vcpus: int = 0
    reclaimable_memory_mb: int = 0


class RecommendationPatch(BaseModel):
    status: RecommendationStatus


class RecommendationPage(BaseModel):
    items: list[RecommendationOut]
    total: int
    limit: int
    offset: int
    total_reclaimable_vcpus: int
    total_reclaimable_memory_mb: int


class MlStatus(BaseModel):
    anomaly: dict[str, Any]
    forecast: dict[str, Any]
    rightsizing: dict[str, Any]
    artifact_dir: str
    loaded_at: datetime | None
