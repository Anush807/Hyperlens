"""Normalized ingest contract. Mirrors services/collector/app/schemas.py (spec 6.1).

The two copies are intentional: neither service imports the other's package. The
API test suite asserts the two models stay field-compatible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    VSPHERE = "vsphere"
    NUTANIX = "nutanix"


class ClusterIn(BaseModel):
    external_id: str
    name: str


class HostIn(BaseModel):
    external_id: str
    cluster_external_id: str | None = None
    name: str
    cpu_cores: int = Field(ge=0)
    cpu_mhz_per_core: int | None = None
    memory_mb: int = Field(ge=0)
    power_state: Literal["on", "off", "unknown"]


class VMIn(BaseModel):
    external_id: str
    host_external_id: str | None = None
    name: str
    vcpus: int = Field(ge=0)
    memory_mb: int = Field(ge=0)
    provisioned_storage_gb: float = Field(ge=0)
    guest_os: str | None = None
    power_state: Literal["on", "off", "suspended", "unknown"]


class DatastoreIn(BaseModel):
    external_id: str
    name: str
    capacity_gb: float = Field(ge=0)
    free_gb: float = Field(ge=0)


class InventoryBatch(BaseModel):
    source_id: uuid.UUID
    collected_at: datetime
    clusters: list[ClusterIn] = Field(default_factory=list)
    hosts: list[HostIn] = Field(default_factory=list)
    vms: list[VMIn] = Field(default_factory=list)
    datastores: list[DatastoreIn] = Field(default_factory=list)


class MetricPoint(BaseModel):
    entity_type: Literal["vm", "host"]
    entity_external_id: str
    ts: datetime
    cpu_pct: float
    mem_pct: float
    disk_iops: float
    disk_latency_ms: float
    net_kbps: float


class MetricsBatch(BaseModel):
    source_id: uuid.UUID
    points: list[MetricPoint] = Field(default_factory=list, max_length=20000)


class SourceStatusIn(BaseModel):
    status: Literal["ok", "failed"]
    error: str | None = None
    collected_at: datetime | None = None


class InternalSourceOut(BaseModel):
    """What the collector receives - this is the only place a password is decrypted."""

    id: uuid.UUID
    name: str
    type: SourceType
    endpoint: str
    username: str
    password: str
    verify_ssl: bool


class MetricsWatermarks(BaseModel):
    """Newest ingested metric timestamp per source id, null where none exists.

    The collector has no database of its own, so this is how it learns how far
    behind it is after an outage.
    """

    watermarks: dict[uuid.UUID, datetime | None]


class IngestResult(BaseModel):
    clusters: int = 0
    hosts: int = 0
    vms: int = 0
    datastores: int = 0
    soft_deleted_vms: int = 0
    points: int = 0
