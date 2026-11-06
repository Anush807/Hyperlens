"""Nutanix Prism v3 adapter.

Talks the real Prism Central v3 list API (POST .../list with kind/length/offset
and a paginated `metadata.total_matches` envelope), so pointing it at a real
Prism Central means changing the endpoint and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.adapters.base import AdapterError, HypervisorAdapter
from app.config import settings
from app.schemas import (
    NUTANIX_POWER_STATES,
    ClusterIn,
    DatastoreIn,
    HostIn,
    HostPowerState,
    SourceConfig,
    VMIn,
    normalize_power_state,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 500
MAX_PAGES = 50  # hard stop: a mis-reported total_matches must not loop forever


class NutanixAdapter(HypervisorAdapter):
    def __init__(self, source: SourceConfig) -> None:
        super().__init__(source)
        self._client: httpx.Client | None = None

    def connect(self) -> None:
        self._client = httpx.Client(
            base_url=self.source.endpoint.rstrip("/"),
            auth=(self.source.username, self.source.password),
            verify=self.source.verify_ssl,
            timeout=settings.http_timeout_seconds,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- paging -----------------------------------------------------------

    def _list(self, kind: str) -> list[dict[str, Any]]:
        if self._client is None:
            raise AdapterError("adapter is not connected")

        path = f"/api/nutanix/v3/{kind}s/list"
        entities: list[dict[str, Any]] = []
        offset = 0

        for _ in range(MAX_PAGES):
            try:
                response = self._client.post(
                    path, json={"kind": kind, "length": PAGE_SIZE, "offset": offset}
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                raise AdapterError(f"Prism returned {exc.response.status_code} for {path}") from exc
            except httpx.HTTPError as exc:
                raise AdapterError(f"Prism request to {path} failed: {exc}") from exc
            except ValueError as exc:
                raise AdapterError(f"Prism returned a non-JSON body for {path}") from exc

            page = payload.get("entities") or []
            entities.extend(page)

            metadata = payload.get("metadata") or {}
            total = int(metadata.get("total_matches") or len(entities))
            offset += len(page)
            if not page or offset >= total:
                break

        return entities

    # --- mapping ----------------------------------------------------------

    @staticmethod
    def _uuid(entity: dict[str, Any]) -> str:
        return str((entity.get("metadata") or {}).get("uuid") or "")

    @staticmethod
    def _name(entity: dict[str, Any], fallback: str) -> str:
        spec_name = (entity.get("spec") or {}).get("name")
        status_name = (entity.get("status") or {}).get("name")
        return str(spec_name or status_name or fallback)

    @staticmethod
    def _resources(entity: dict[str, Any]) -> dict[str, Any]:
        """Prism reports desired state under spec and actual under status;
        status wins where both exist."""
        spec = ((entity.get("spec") or {}).get("resources")) or {}
        status = ((entity.get("status") or {}).get("resources")) or {}
        return {**spec, **{k: v for k, v in status.items() if v is not None}}

    def list_clusters(self) -> list[ClusterIn]:
        out = []
        for entity in self._list("cluster"):
            external_id = self._uuid(entity)
            if not external_id:
                continue
            out.append(ClusterIn(external_id=external_id, name=self._name(entity, external_id)))
        return out

    def list_hosts(self) -> list[HostIn]:
        out = []
        for entity in self._list("host"):
            external_id = self._uuid(entity)
            if not external_id:
                continue
            resources = self._resources(entity)
            cluster_ref = (
                (entity.get("status") or {}).get("cluster_reference")
                or (entity.get("spec") or {}).get("cluster_reference")
                or {}
            )
            cpu_model = resources.get("cpu_model") or {}
            out.append(
                HostIn(
                    external_id=external_id,
                    cluster_external_id=cluster_ref.get("uuid"),
                    name=self._name(entity, external_id),
                    cpu_cores=_int(resources.get("num_cpu_cores")),
                    cpu_mhz_per_core=_int(
                        resources.get("cpu_frequency_hz", 0) / 1_000_000
                        if resources.get("cpu_frequency_hz")
                        else cpu_model.get("frequency_mhz")
                    )
                    or None,
                    memory_mb=_int(resources.get("memory_capacity_mib")),
                    power_state=_host_power(resources),
                )
            )
        return out

    def list_vms(self) -> list[VMIn]:
        out = []
        for entity in self._list("vm"):
            external_id = self._uuid(entity)
            if not external_id:
                continue
            resources = self._resources(entity)
            sockets = _int(resources.get("num_sockets"), 1)
            per_socket = _int(resources.get("num_vcpus_per_socket"), 1)
            host_ref = resources.get("host_reference") or {}

            disks = resources.get("disk_list") or []
            total_bytes = sum(
                _int(disk.get("disk_size_bytes")) or _int(disk.get("disk_size_mib")) * 1024 * 1024
                for disk in disks
                if isinstance(disk, dict)
            )

            out.append(
                VMIn(
                    external_id=external_id,
                    host_external_id=host_ref.get("uuid"),
                    name=self._name(entity, external_id),
                    vcpus=max(0, sockets * per_socket),
                    memory_mb=_int(resources.get("memory_size_mib")),
                    provisioned_storage_gb=round(total_bytes / (1024**3), 2),
                    guest_os=(resources.get("guest_os") or {}).get("name")
                    if isinstance(resources.get("guest_os"), dict)
                    else resources.get("guest_os"),
                    power_state=normalize_power_state(
                        resources.get("power_state"), NUTANIX_POWER_STATES
                    ),
                )
            )
        return out

    def list_datastores(self) -> list[DatastoreIn]:
        """Nutanix has storage containers rather than datastores. The mock may
        expose them; a Prism Central that does not simply yields an empty list
        (spec 7.3)."""
        try:
            entities = self._list("storage_container")
        except AdapterError as exc:
            log.info("no storage containers available: %s", exc)
            return []

        out = []
        for entity in entities:
            external_id = self._uuid(entity)
            if not external_id:
                continue
            resources = self._resources(entity)
            capacity_gb = _float(resources.get("capacity_bytes")) / (1024**3)
            free_gb = _float(resources.get("free_bytes")) / (1024**3)
            out.append(
                DatastoreIn(
                    external_id=external_id,
                    name=self._name(entity, external_id),
                    capacity_gb=round(capacity_gb, 2),
                    free_gb=round(min(free_gb, capacity_gb), 2),
                )
            )
        return out


def _host_power(resources: dict[str, Any]) -> HostPowerState:
    """Prism reports host liveness as `host_state`, where NORMAL means up."""
    raw = str(resources.get("power_state") or resources.get("host_state") or "NORMAL").upper()
    if raw in {"NORMAL", "ON", "POWERED_ON"}:
        return "on"
    if raw in {"OFF", "POWERED_OFF"}:
        return "off"
    return "unknown"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
