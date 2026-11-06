"""Nutanix adapter against recorded Prism v3 responses."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.adapters.base import AdapterError
from app.adapters.nutanix import NutanixAdapter

BASE = "http://nutanix-mock:9440"


def _envelope(entities: list[dict], total: int, offset: int = 0) -> dict:
    return {
        "api_version": "3.1",
        "metadata": {"total_matches": total, "length": len(entities), "offset": offset},
        "entities": entities,
    }


def _vm(uuid_: str, name: str, sockets: int, per_socket: int, memory: int, power: str) -> dict:
    return {
        "status": {
            "name": name,
            "resources": {
                "power_state": power,
                "num_sockets": sockets,
                "num_vcpus_per_socket": per_socket,
                "memory_size_mib": memory,
                "host_reference": {"kind": "host", "uuid": "host-1"},
                "guest_os": "Ubuntu 22.04 LTS",
                "disk_list": [{"disk_size_bytes": 50 * 1024**3}],
            },
        },
        "spec": {"name": name, "resources": {}},
        "metadata": {"kind": "vm", "uuid": uuid_},
    }


@respx.mock
def test_list_vms_maps_prism_fields(nutanix_source):
    respx.post(f"{BASE}/api/nutanix/v3/vms/list").mock(
        return_value=httpx.Response(
            200, json=_envelope([_vm("vm-1", "nx-vm-001", 2, 4, 16384, "ON")], 1)
        )
    )

    with NutanixAdapter(nutanix_source) as adapter:
        vms = adapter.list_vms()

    assert len(vms) == 1
    vm = vms[0]
    assert vm.external_id == "vm-1"
    assert vm.name == "nx-vm-001"
    # num_sockets * num_vcpus_per_socket, per spec 7.3
    assert vm.vcpus == 8
    assert vm.memory_mb == 16384
    assert vm.provisioned_storage_gb == 50.0
    assert vm.power_state == "on"
    assert vm.host_external_id == "host-1"


@respx.mock
@pytest.mark.parametrize(
    ("prism_state", "expected"),
    [("ON", "on"), ("OFF", "off"), ("SUSPENDED", "suspended"), ("WHO_KNOWS", "unknown")],
)
def test_power_state_normalisation(nutanix_source, prism_state, expected):
    respx.post(f"{BASE}/api/nutanix/v3/vms/list").mock(
        return_value=httpx.Response(
            200, json=_envelope([_vm("vm-1", "a", 1, 1, 1024, prism_state)], 1)
        )
    )
    with NutanixAdapter(nutanix_source) as adapter:
        assert adapter.list_vms()[0].power_state == expected


@respx.mock
def test_pagination_follows_total_matches(nutanix_source):
    page_one = [_vm(f"vm-{i}", f"nx-{i}", 1, 2, 4096, "ON") for i in range(500)]
    page_two = [_vm(f"vm-{i}", f"nx-{i}", 1, 2, 4096, "ON") for i in range(500, 625)]
    route = respx.post(f"{BASE}/api/nutanix/v3/vms/list")
    route.side_effect = [
        httpx.Response(200, json=_envelope(page_one, 625, 0)),
        httpx.Response(200, json=_envelope(page_two, 625, 500)),
    ]

    with NutanixAdapter(nutanix_source) as adapter:
        vms = adapter.list_vms()

    assert len(vms) == 625
    assert route.call_count == 2
    # The second request must carry the advanced offset.
    import json as _json

    assert _json.loads(route.calls[1].request.read())["offset"] == 500


@respx.mock
def test_empty_page_stops_paging(nutanix_source):
    """A total_matches that over-reports must not spin forever."""
    route = respx.post(f"{BASE}/api/nutanix/v3/vms/list")
    route.side_effect = [
        httpx.Response(200, json=_envelope([_vm("vm-1", "a", 1, 1, 1024, "ON")], 9999, 0)),
        httpx.Response(200, json=_envelope([], 9999, 1)),
    ]
    with NutanixAdapter(nutanix_source) as adapter:
        assert len(adapter.list_vms()) == 1
    assert route.call_count == 2


@respx.mock
def test_http_error_becomes_adapter_error(nutanix_source):
    respx.post(f"{BASE}/api/nutanix/v3/clusters/list").mock(
        return_value=httpx.Response(401, json={"message": "nope"})
    )
    with NutanixAdapter(nutanix_source) as adapter, pytest.raises(AdapterError, match="401"):
        adapter.list_clusters()


@respx.mock
def test_missing_storage_containers_yields_empty_list(nutanix_source):
    """Prism Central without containers must not fail the whole collection."""
    respx.post(f"{BASE}/api/nutanix/v3/storage_containers/list").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with NutanixAdapter(nutanix_source) as adapter:
        assert adapter.list_datastores() == []


@respx.mock
def test_hosts_map_cluster_reference(nutanix_source):
    host = {
        "status": {
            "name": "nx-node-01",
            "cluster_reference": {"kind": "cluster", "uuid": "cluster-1"},
            "resources": {
                "num_cpu_cores": 24,
                "memory_capacity_mib": 393216,
                "cpu_frequency_hz": 3_000_000_000,
                "power_state": "ON",
            },
        },
        "spec": {"name": "nx-node-01", "resources": {}},
        "metadata": {"kind": "host", "uuid": "host-1"},
    }
    respx.post(f"{BASE}/api/nutanix/v3/hosts/list").mock(
        return_value=httpx.Response(200, json=_envelope([host], 1))
    )
    with NutanixAdapter(nutanix_source) as adapter:
        hosts = adapter.list_hosts()

    assert hosts[0].cluster_external_id == "cluster-1"
    assert hosts[0].cpu_cores == 24
    assert hosts[0].cpu_mhz_per_core == 3000
    assert hosts[0].power_state == "on"


@respx.mock
def test_entities_without_uuid_are_skipped(nutanix_source):
    broken = {"status": {"name": "ghost", "resources": {}}, "spec": {}, "metadata": {"kind": "vm"}}
    respx.post(f"{BASE}/api/nutanix/v3/vms/list").mock(
        return_value=httpx.Response(200, json=_envelope([broken], 1))
    )
    with NutanixAdapter(nutanix_source) as adapter:
        assert adapter.list_vms() == []
