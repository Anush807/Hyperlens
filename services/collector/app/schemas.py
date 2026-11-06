"""Normalized models every adapter returns (spec 6.1).

Mirrors services/api/app/schemas/ingest.py. Keeping two copies is deliberate:
neither service depends on the other's package, and the contract is small enough
that a test asserting field parity is cheaper than a shared library.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast

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
    points: list[MetricPoint] = Field(default_factory=list)


class SourceConfig(BaseModel):
    """What GET /internal/sources hands back, password already decrypted."""

    id: uuid.UUID
    name: str
    type: SourceType
    endpoint: str
    username: str
    password: str
    verify_ssl: bool = False


# Power-state vocabularies differ per hypervisor; normalize once, here.
VSPHERE_POWER_STATES = {
    "poweredOn": "on",
    "poweredOff": "off",
    "suspended": "suspended",
}

NUTANIX_POWER_STATES = {
    "ON": "on",
    "OFF": "off",
    "POWERED_ON": "on",
    "POWERED_OFF": "off",
    "SUSPENDED": "suspended",
    "PAUSED": "suspended",
}


EntityPowerState = Literal["on", "off", "suspended", "unknown"]
HostPowerState = Literal["on", "off", "unknown"]


def normalize_power_state(raw: object, mapping: dict[str, str]) -> EntityPowerState:
    """Vendor string to the normalized vocabulary. Anything unrecognised is
    `unknown` rather than a guess."""
    return cast(EntityPowerState, mapping.get(str(raw), "unknown"))


def normalize_host_power_state(raw: object, mapping: dict[str, str]) -> HostPowerState:
    """`HostIn` has no `suspended`: a standby host is reported as unknown."""
    state = normalize_power_state(raw, mapping)
    return cast(HostPowerState, state if state in {"on", "off"} else "unknown")
