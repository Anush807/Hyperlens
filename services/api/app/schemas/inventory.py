from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import PowerState, SourceType
from app.schemas.ml import AnomalyOut, RecommendationOut


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ClusterOut(_Base):
    id: uuid.UUID
    source_id: uuid.UUID
    external_id: str
    name: str
    host_count: int = 0
    vm_count: int = 0


class HostOut(_Base):
    id: uuid.UUID
    source_id: uuid.UUID
    cluster_id: uuid.UUID | None
    cluster_name: str | None = None
    external_id: str
    name: str
    cpu_cores: int
    cpu_mhz_per_core: int | None
    memory_mb: int
    power_state: PowerState
    last_seen_at: datetime | None
    vm_count: int = 0


class VMOut(_Base):
    id: uuid.UUID
    source_id: uuid.UUID
    source_type: SourceType | None = None
    host_id: uuid.UUID | None
    host_name: str | None = None
    cluster_id: uuid.UUID | None = None
    cluster_name: str | None = None
    external_id: str
    name: str
    vcpus: int
    memory_mb: int
    provisioned_storage_gb: float
    guest_os: str | None
    power_state: PowerState
    last_seen_at: datetime | None
    is_deleted: bool


class DatastoreOut(_Base):
    id: uuid.UUID
    source_id: uuid.UUID
    external_id: str
    name: str
    capacity_gb: float
    free_gb: float
    used_pct: float


class LatestMetrics(BaseModel):
    ts: datetime | None = None
    cpu_pct: float | None = None
    mem_pct: float | None = None
    disk_iops: float | None = None
    disk_latency_ms: float | None = None
    net_kbps: float | None = None


class VMDetail(BaseModel):
    """`GET /vms/{id}`: the specs plus the three things an operator wants next
    to them. A declared model rather than a bare dict, so the frontend's types
    stay fully generated (spec 11.2)."""

    vm: VMOut
    latest_metrics: LatestMetrics
    open_anomalies: list[AnomalyOut]
    open_recommendations: list[RecommendationOut]


class ClusterAllocation(BaseModel):
    cluster_id: uuid.UUID | None
    cluster_name: str
    source_type: SourceType
    host_count: int
    vm_count: int
    physical_cores: int
    allocated_vcpus: int
    vcpu_overcommit_ratio: float
    physical_memory_mb: int
    allocated_memory_mb: int
    memory_overcommit_ratio: float


class InventorySummary(BaseModel):
    sources: int
    clusters: int
    hosts: int
    vms: int
    vms_powered_on: int
    datastores: int
    total_vcpus: int
    total_vm_memory_mb: int
    total_physical_cores: int
    total_physical_memory_mb: int
    total_storage_capacity_gb: float
    total_storage_free_gb: float
    open_anomalies: int
    open_recommendations: int
    reclaimable_vcpus: int
    reclaimable_memory_mb: int
    per_cluster: list[ClusterAllocation]
