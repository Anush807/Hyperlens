"""vSphere adapter mapping, with pyVmomi managed objects stubbed.

Real pyVmomi objects cannot be constructed outside a connection, so the tests
stand in simple namespaces - which also proves the adapter only touches the
properties it documents.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.vsphere import VSphereAdapter, _get


class FakeMo(SimpleNamespace):
    """A managed object with the `_moId` the adapter uses as external_id."""

    def __init__(self, mo_id: str, **kwargs):
        super().__init__(**kwargs)
        self._moId = mo_id


def _adapter(source, **views) -> VSphereAdapter:
    adapter = VSphereAdapter(source)
    adapter._content = object()  # bypass connect(); _view is monkeypatched below
    adapter._view = lambda vim_type: views.get(vim_type.__name__, [])  # type: ignore[assignment]
    return adapter


class _VimTypes:
    class ClusterComputeResource: ...

    class HostSystem: ...

    class VirtualMachine: ...

    class Datastore: ...


@pytest.fixture(autouse=True)
def patch_vim(monkeypatch):
    monkeypatch.setattr("app.adapters.vsphere.vim", _VimTypes)


def test_get_returns_default_for_missing_nested_attribute():
    obj = SimpleNamespace(summary=SimpleNamespace(config=None))
    assert _get(obj, "summary.config.numCpu", 7) == 7
    assert _get(obj, "nothing.at.all") is None


def test_clusters_build_the_host_to_cluster_map(vsphere_source):
    host = FakeMo("host-1")
    cluster = FakeMo("domain-c1", name="DC0_C0", host=[host])
    adapter = _adapter(vsphere_source, ClusterComputeResource=[cluster])

    clusters = adapter.list_clusters()

    assert [c.external_id for c in clusters] == ["domain-c1"]
    assert clusters[0].name == "DC0_C0"
    assert adapter._host_to_cluster == {"host-1": "domain-c1"}


def test_hosts_convert_memory_to_mb_and_attach_cluster(vsphere_source):
    host = FakeMo(
        "host-1",
        name="esx-01",
        summary=SimpleNamespace(
            hardware=SimpleNamespace(
                numCpuCores=16, numCpuThreads=32, cpuMhz=2600, memorySize=68719476736
            )
        ),
        runtime=SimpleNamespace(powerState="poweredOn"),
    )
    cluster = FakeMo("domain-c1", name="DC0_C0", host=[host])
    adapter = _adapter(vsphere_source, ClusterComputeResource=[cluster], HostSystem=[host])

    hosts = adapter.list_hosts()

    assert hosts[0].cpu_cores == 16
    assert hosts[0].cpu_mhz_per_core == 2600
    assert hosts[0].memory_mb == 65536  # 64 GiB
    assert hosts[0].cluster_external_id == "domain-c1"
    assert hosts[0].power_state == "on"


def test_host_suspended_state_is_reported_as_unknown(vsphere_source):
    """HostIn has no `suspended` member, so a standby host must not crash."""
    host = FakeMo(
        "host-1",
        name="esx-01",
        summary=SimpleNamespace(hardware=SimpleNamespace(numCpuCores=8, memorySize=0)),
        runtime=SimpleNamespace(powerState="suspended"),
    )
    adapter = _adapter(vsphere_source, HostSystem=[host])
    assert adapter.list_hosts()[0].power_state == "unknown"


def test_vms_sum_committed_and_uncommitted_storage(vsphere_source):
    vm = FakeMo(
        "vm-42",
        name="fallback",
        summary=SimpleNamespace(
            config=SimpleNamespace(
                name="DC0_H0_VM0",
                numCpu=4,
                memorySizeMB=8192,
                guestFullName="CentOS 8",
                guestId="centos8",
            ),
            storage=SimpleNamespace(committed=20 * 1024**3, uncommitted=10 * 1024**3),
        ),
        runtime=SimpleNamespace(powerState="poweredOn", host=FakeMo("host-1")),
    )
    adapter = _adapter(vsphere_source, VirtualMachine=[vm])

    vms = adapter.list_vms()

    assert vms[0].external_id == "vm-42"
    assert vms[0].name == "DC0_H0_VM0"
    assert vms[0].vcpus == 4
    assert vms[0].provisioned_storage_gb == 30.0
    assert vms[0].host_external_id == "host-1"
    assert vms[0].guest_os == "CentOS 8"


def test_vm_tolerates_vcsim_shaped_gaps(vsphere_source):
    """vcsim leaves optional properties unset; the mapping must still produce a
    valid record rather than raising (spec 16)."""
    vm = FakeMo(
        "vm-1",
        name="sparse-vm",
        summary=SimpleNamespace(config=None, storage=None),
        runtime=SimpleNamespace(powerState=None, host=None),
    )
    adapter = _adapter(vsphere_source, VirtualMachine=[vm])

    result = adapter.list_vms()[0]

    assert result.name == "sparse-vm"
    assert result.vcpus == 0
    assert result.memory_mb == 0
    assert result.provisioned_storage_gb == 0.0
    assert result.power_state == "unknown"
    assert result.host_external_id is None


def test_datastore_free_never_exceeds_capacity(vsphere_source):
    datastore = FakeMo(
        "datastore-1",
        name="fallback",
        summary=SimpleNamespace(name="LocalDS_0", capacity=100 * 1024**3, freeSpace=150 * 1024**3),
    )
    adapter = _adapter(vsphere_source, Datastore=[datastore])

    result = adapter.list_datastores()[0]

    assert result.name == "LocalDS_0"
    assert result.capacity_gb == 100.0
    assert result.free_gb == 100.0


def test_endpoint_parsing_splits_host_and_port(vsphere_source):
    vsphere_source.endpoint = "https://vcenter.example.com:8443/"
    assert VSphereAdapter(vsphere_source)._split_endpoint() == ("vcenter.example.com", 8443)

    vsphere_source.endpoint = "vcsim"
    host, port = VSphereAdapter(vsphere_source)._split_endpoint()
    assert host == "vcsim" and port > 0
