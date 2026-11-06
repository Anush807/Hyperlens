"""Inventory reads: filtering, pagination, the summary and VM detail."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import auth_header


async def _ingest(client, source, service_header, *, vms: int = 5) -> None:
    payload = {
        "source_id": str(source.id),
        "collected_at": datetime.now(UTC).isoformat(),
        "clusters": [{"external_id": "c1", "name": "DC0_C0"}],
        "hosts": [
            {
                "external_id": "h1",
                "cluster_external_id": "c1",
                "name": "esx-01",
                "cpu_cores": 16,
                "cpu_mhz_per_core": 2600,
                "memory_mb": 65536,
                "power_state": "on",
            }
        ],
        "vms": [
            {
                "external_id": f"v{i}",
                "host_external_id": "h1",
                "name": f"web-{i:02d}" if i % 2 else f"db-{i:02d}",
                "vcpus": 2 + i,
                "memory_mb": 4096 * (i + 1),
                "provisioned_storage_gb": 40.0,
                "guest_os": "Ubuntu 22.04" if i % 2 else "Windows Server 2022",
                "power_state": "on" if i % 3 else "off",
            }
            for i in range(vms)
        ],
        "datastores": [
            {"external_id": "d1", "name": "LocalDS_0", "capacity_gb": 1000.0, "free_gb": 250.0}
        ],
    }
    response = await client.post("/internal/inventory", json=payload, headers=service_header)
    assert response.status_code == 200


async def test_lists_require_authentication(client):
    for path in ("/vms", "/hosts", "/clusters", "/datastores", "/inventory/summary"):
        assert (await client.get(path)).status_code == 401, path


async def test_vm_list_envelope_and_pagination(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=12)

    first = await client.get("/vms", params={"limit": 5}, headers=auth_header(viewer))
    body = first.json()

    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 12
    assert len(body["items"]) == 5
    assert body["offset"] == 0

    second = (
        await client.get("/vms", params={"limit": 5, "offset": 5}, headers=auth_header(viewer))
    ).json()
    assert len(second["items"]) == 5
    assert {v["id"] for v in body["items"]}.isdisjoint({v["id"] for v in second["items"]})

    last = (
        await client.get("/vms", params={"limit": 5, "offset": 10}, headers=auth_header(viewer))
    ).json()
    assert len(last["items"]) == 2


@pytest.mark.parametrize("limit", [0, 501])
async def test_pagination_limits_are_enforced(client, viewer, limit):
    response = await client.get("/vms", params={"limit": limit}, headers=auth_header(viewer))
    assert response.status_code == 422


async def test_vm_search_matches_name_and_guest_os(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=6)

    by_name = (await client.get("/vms", params={"q": "web"}, headers=auth_header(viewer))).json()
    assert by_name["total"] == 3
    assert all("web" in vm["name"] for vm in by_name["items"])

    by_os = (await client.get("/vms", params={"q": "Windows"}, headers=auth_header(viewer))).json()
    assert by_os["total"] == 3


async def test_vm_power_state_filter(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=6)

    powered_on = (
        await client.get("/vms", params={"power_state": "on"}, headers=auth_header(viewer))
    ).json()
    assert powered_on["total"] == 4
    assert all(vm["power_state"] == "on" for vm in powered_on["items"])


async def test_deleted_vms_are_hidden_unless_asked_for(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=4)
    await _ingest(client, source, service_header, vms=2)  # v2, v3 disappear

    default = (await client.get("/vms", headers=auth_header(viewer))).json()
    assert default["total"] == 2

    including = (
        await client.get("/vms", params={"include_deleted": True}, headers=auth_header(viewer))
    ).json()
    assert including["total"] == 4


async def test_vm_rows_carry_source_cluster_and_host(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=1)

    vm = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]
    assert vm["source_type"] == "vsphere"
    assert vm["cluster_name"] == "DC0_C0"
    assert vm["host_name"] == "esx-01"


async def test_vm_detail_bundles_metrics_anomalies_and_recommendations(
    client, viewer, source, service_header, mongo
):
    await _ingest(client, source, service_header, vms=1)
    vm = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]

    await client.post(
        "/internal/metrics",
        json={
            "source_id": str(source.id),
            "points": [
                {
                    "entity_type": "vm",
                    "entity_external_id": "v0",
                    "ts": datetime.now(UTC).isoformat(),
                    "cpu_pct": 42.5,
                    "mem_pct": 61.0,
                    "disk_iops": 120.0,
                    "disk_latency_ms": 2.5,
                    "net_kbps": 900.0,
                }
            ],
        },
        headers=service_header,
    )

    detail = (await client.get(f"/vms/{vm['id']}", headers=auth_header(viewer))).json()

    assert detail["vm"]["id"] == vm["id"]
    assert detail["latest_metrics"]["cpu_pct"] == 42.5
    assert detail["open_anomalies"] == []
    assert detail["open_recommendations"] == []


async def test_unknown_vm_is_a_clean_404(client, viewer):
    response = await client.get(
        "/vms/11111111-1111-1111-1111-111111111111", headers=auth_header(viewer)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_host_and_cluster_lists_carry_counts(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=5)

    host = (await client.get("/hosts", headers=auth_header(viewer))).json()["items"][0]
    assert host["vm_count"] == 5
    assert host["cluster_name"] == "DC0_C0"

    cluster = (await client.get("/clusters", headers=auth_header(viewer))).json()["items"][0]
    assert cluster["host_count"] == 1
    assert cluster["vm_count"] == 5


async def test_datastore_used_pct_is_derived(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=1)

    datastore = (await client.get("/datastores", headers=auth_header(viewer))).json()["items"][0]
    assert datastore["capacity_gb"] == 1000.0
    assert datastore["used_pct"] == 75.0


async def test_summary_totals_and_allocation(client, viewer, source, service_header):
    await _ingest(client, source, service_header, vms=4)

    summary = (await client.get("/inventory/summary", headers=auth_header(viewer))).json()

    assert summary["vms"] == 4
    assert summary["hosts"] == 1
    assert summary["clusters"] == 1
    assert summary["vms_powered_on"] == 2
    assert summary["total_vcpus"] == 2 + 3 + 4 + 5
    assert summary["total_physical_cores"] == 16

    allocation = summary["per_cluster"][0]
    assert allocation["cluster_name"] == "DC0_C0"
    assert allocation["physical_cores"] == 16
    assert allocation["allocated_vcpus"] == 14
    assert allocation["vcpu_overcommit_ratio"] == pytest.approx(0.88, abs=0.01)


async def test_summary_is_cached_for_a_minute(client, viewer, source, service_header):
    """The dashboard polls this and it touches every inventory table."""
    await _ingest(client, source, service_header, vms=2)
    first = (await client.get("/inventory/summary", headers=auth_header(viewer))).json()

    await _ingest(client, source, service_header, vms=6)
    cached = (await client.get("/inventory/summary", headers=auth_header(viewer))).json()
    assert cached["vms"] == first["vms"] == 2

    from app.cache import invalidate

    invalidate()
    fresh = (await client.get("/inventory/summary", headers=auth_header(viewer))).json()
    assert fresh["vms"] == 6


async def test_summary_on_an_empty_estate_returns_zeroes(client, viewer):
    summary = (await client.get("/inventory/summary", headers=auth_header(viewer))).json()
    assert summary["vms"] == 0
    assert summary["per_cluster"] == []
    assert summary["reclaimable_vcpus"] == 0
