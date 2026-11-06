"""Adapter factory keyed by source type (spec 7.1)."""

from __future__ import annotations

from app.adapters.base import AdapterError, HypervisorAdapter
from app.adapters.nutanix import NutanixAdapter
from app.adapters.vsphere import VSphereAdapter
from app.schemas import SourceConfig, SourceType

_REGISTRY: dict[SourceType, type[HypervisorAdapter]] = {
    SourceType.VSPHERE: VSphereAdapter,
    SourceType.NUTANIX: NutanixAdapter,
}


def get_adapter(source: SourceConfig) -> HypervisorAdapter:
    adapter_cls = _REGISTRY.get(SourceType(source.type))
    if adapter_cls is None:
        raise AdapterError(f"no adapter registered for source type {source.type!r}")
    return adapter_cls(source)


def supported_types() -> list[str]:
    return [str(t) for t in _REGISTRY]
