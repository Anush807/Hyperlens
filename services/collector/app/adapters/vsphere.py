"""vSphere adapter over pyVmomi.

Every pyVmomi property access is wrapped in a defensive getter: vcsim does not
populate all the fields a real vCenter does, and a missing optional property
must not take the whole collection down (spec 16).
"""

from __future__ import annotations

import logging
import ssl
import time
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

from app.adapters.base import AdapterError, HypervisorAdapter
from app.config import settings
from app.schemas import (
    VSPHERE_POWER_STATES,
    ClusterIn,
    DatastoreIn,
    HostIn,
    HostPowerState,
    SourceConfig,
    VMIn,
    normalize_host_power_state,
    normalize_power_state,
)

log = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.5
_insecure_warned = False


def _get(obj: Any, path: str, default: Any = None) -> Any:
    """Safe dotted attribute access over managed objects."""
    current = obj
    for part in path.split("."):
        current = getattr(current, part, None)
        if current is None:
            return default
    return current


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


class VSphereAdapter(HypervisorAdapter):
    def __init__(self, source: SourceConfig) -> None:
        super().__init__(source)
        self._si: Any = None
        self._content: Any = None
        self._host_to_cluster: dict[str, str] = {}

    # --- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        global _insecure_warned
        host, port = self._split_endpoint()
        context: ssl.SSLContext | None = None
        if not self.source.verify_ssl:
            context = ssl._create_unverified_context()
            if not _insecure_warned:
                log.warning(
                    "TLS verification disabled for vSphere connections "
                    "(set verify_ssl on the source to enable it)"
                )
                _insecure_warned = True

        last_error: Exception | None = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                self._si = SmartConnect(
                    host=host,
                    port=port,
                    user=self.source.username,
                    pwd=self.source.password,
                    sslContext=context,
                    connectionPoolTimeout=int(settings.http_timeout_seconds),
                )
                self._content = self._si.RetrieveContent()
                log.info("connected to vSphere", extra={"host": host, "attempt": attempt})
                return
            except Exception as exc:  # noqa: BLE001 - pyVmomi raises many types
                last_error = exc
                if attempt < CONNECT_ATTEMPTS:
                    delay = BACKOFF_BASE_SECONDS**attempt
                    log.warning(
                        "vSphere connect failed, retrying",
                        extra={"host": host, "attempt": attempt, "delay": delay},
                    )
                    time.sleep(delay)

        raise AdapterError(f"could not connect to vSphere at {host}:{port}: {last_error}")

    def close(self) -> None:
        if self._si is not None:
            try:
                Disconnect(self._si)
            except Exception:  # noqa: BLE001 - disconnect failures are not actionable
                log.debug("vSphere disconnect raised", exc_info=True)
            finally:
                self._si = None
                self._content = None

    def _split_endpoint(self) -> tuple[str, int]:
        endpoint = self.source.endpoint.replace("https://", "").replace("http://", "").strip("/")
        if ":" in endpoint:
            host, _, port = endpoint.partition(":")
            return host, _int(port, settings.vsphere_port)
        return endpoint, settings.vsphere_port

    # --- inventory --------------------------------------------------------

    def _view(self, vim_type: Any) -> list[Any]:
        if self._content is None:
            raise AdapterError("adapter is not connected")
        view = self._content.viewManager.CreateContainerView(
            self._content.rootFolder, [vim_type], True
        )
        try:
            return list(view.view)
        finally:
            try:
                view.Destroy()
            except Exception:  # noqa: BLE001
                log.debug("container view destroy failed", exc_info=True)

    def list_clusters(self) -> list[ClusterIn]:
        clusters: list[ClusterIn] = []
        # Build the host -> cluster map here so list_hosts() does not re-walk.
        self._host_to_cluster = {}
        for obj in self._view(vim.ClusterComputeResource):
            external_id = str(obj._moId)
            clusters.append(ClusterIn(external_id=external_id, name=_get(obj, "name", external_id)))
            for host in _get(obj, "host", []) or []:
                self._host_to_cluster[str(host._moId)] = external_id
        return clusters

    def list_hosts(self) -> list[HostIn]:
        if not self._host_to_cluster:
            self.list_clusters()

        hosts: list[HostIn] = []
        for obj in self._view(vim.HostSystem):
            external_id = str(obj._moId)
            hardware = _get(obj, "summary.hardware")
            cores = _int(_get(hardware, "numCpuCores"))
            threads = _int(_get(hardware, "numCpuThreads"))
            hosts.append(
                HostIn(
                    external_id=external_id,
                    cluster_external_id=self._host_to_cluster.get(external_id),
                    name=_get(obj, "name", external_id),
                    cpu_cores=cores or threads,
                    cpu_mhz_per_core=_int(_get(hardware, "cpuMhz")) or None,
                    memory_mb=_int(_float(_get(hardware, "memorySize")) / (1024 * 1024)),
                    power_state=self._host_power_state(obj),
                )
            )
        return hosts

    @staticmethod
    def _host_power_state(obj: Any) -> HostPowerState:
        return normalize_host_power_state(_get(obj, "runtime.powerState"), VSPHERE_POWER_STATES)

    def list_vms(self) -> list[VMIn]:
        vms: list[VMIn] = []
        for obj in self._view(vim.VirtualMachine):
            external_id = str(obj._moId)
            config = _get(obj, "summary.config")
            storage = _get(obj, "summary.storage")
            host = _get(obj, "runtime.host")

            committed = _float(_get(storage, "committed"))
            uncommitted = _float(_get(storage, "uncommitted"))
            provisioned = (committed + uncommitted) / (1024**3)

            vms.append(
                VMIn(
                    external_id=external_id,
                    host_external_id=str(host._moId) if host is not None else None,
                    name=_get(config, "name") or _get(obj, "name", external_id),
                    vcpus=_int(_get(config, "numCpu")),
                    memory_mb=_int(_get(config, "memorySizeMB")),
                    provisioned_storage_gb=round(provisioned, 2),
                    guest_os=_get(config, "guestFullName") or _get(config, "guestId"),
                    power_state=normalize_power_state(
                        _get(obj, "runtime.powerState"), VSPHERE_POWER_STATES
                    ),
                )
            )
        return vms

    def list_datastores(self) -> list[DatastoreIn]:
        datastores: list[DatastoreIn] = []
        for obj in self._view(vim.Datastore):
            external_id = str(obj._moId)
            summary = _get(obj, "summary")
            capacity = _float(_get(summary, "capacity")) / (1024**3)
            free = _float(_get(summary, "freeSpace")) / (1024**3)
            datastores.append(
                DatastoreIn(
                    external_id=external_id,
                    name=_get(summary, "name") or _get(obj, "name", external_id),
                    capacity_gb=round(capacity, 2),
                    free_gb=round(min(free, capacity), 2),
                )
            )
        return datastores
