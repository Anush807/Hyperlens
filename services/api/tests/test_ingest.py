"""Internal ingest: upsert idempotency, soft delete and service-token auth."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import VM, Cluster, Datastore, Host


def batch(source_id, *, vm_ids=("v1", "v2"), collected_at=None, vcpus=4) -> dict:
    return {
        "source_id": str(source_id),
        "collected_at": (collected_at or datetime.now(UTC)).isoformat(),
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
                "external_id": vm_id,
                "host_external_id": "h1",
                "name": f"vm-{vm_id}",
                "vcpus": vcpus,
                "memory_mb": 8192,
                "provisioned_storage_gb": 40.0,
                "guest_os": "Ubuntu 22.04",
                "power_state": "on",
            }
            for vm_id in vm_ids
        ],
        "datastores": [
            {"external_id": "d1", "name": "LocalDS_0", "capacity_gb": 1000.0, "free_gb": 400.0}
        ],
    }


async def test_internal_requires_the_service_token(client, source):
    response = await client.post("/internal/inventory", json=batch(source.id))
    assert response.status_code == 401

    response = await client.post(
        "/internal/inventory", json=batch(source.id), headers={"X-Service-Token": "wrong"}
    )
    assert response.status_code == 401


async def test_internal_routes_are_hidden_from_the_public_schema(client):
    schema = (await client.get("/openapi.json")).json()
    assert not [path for path in schema["paths"] if path.startswith("/internal")]


async def test_inventory_ingest_creates_every_entity(client, source, service_header, session):
    response = await client.post(
        "/internal/inventory", json=batch(source.id), headers=service_header
    )

    assert response.status_code == 200
    assert response.json() == {
        "clusters": 1,
        "hosts": 1,
        "vms": 2,
        "datastores": 1,
        "soft_deleted_vms": 0,
        "points": 0,
    }
    assert (await session.execute(select(func.count(VM.id)))).scalar_one() == 2
    assert (await session.execute(select(func.count(Host.id)))).scalar_one() == 1
    assert (await session.execute(select(func.count(Cluster.id)))).scalar_one() == 1
    assert (await session.execute(select(func.count(Datastore.id)))).scalar_one() == 1


async def test_hosts_are_linked_to_their_cluster(client, source, service_header, session):
    await client.post("/internal/inventory", json=batch(source.id), headers=service_header)

    host = (await session.execute(select(Host))).scalar_one()
    cluster = (await session.execute(select(Cluster))).scalar_one()
    assert host.cluster_id == cluster.id

    vm = (await session.execute(select(VM).limit(1))).scalars().first()
    assert vm.host_id == host.id


async def test_reingesting_the_same_batch_changes_nothing(client, source, service_header, session):
    """Phase 3 acceptance: re-ingesting an identical batch is a no-op."""
    payload = batch(source.id)
    first = (await client.post("/internal/inventory", json=payload, headers=service_header)).json()
    ids_before = set((await session.execute(select(VM.id))).scalars().all())

    second = (await client.post("/internal/inventory", json=payload, headers=service_header)).json()
    ids_after = set((await session.execute(select(VM.id))).scalars().all())

    assert first == second
    assert ids_before == ids_after
    assert (await session.execute(select(func.count(VM.id)))).scalar_one() == 2


async def test_upsert_updates_changed_fields_in_place(client, source, service_header, session):
    await client.post("/internal/inventory", json=batch(source.id), headers=service_header)
    await client.post("/internal/inventory", json=batch(source.id, vcpus=8), headers=service_header)

    vms = (await session.execute(select(VM))).scalars().all()
    assert len(vms) == 2
    assert {vm.vcpus for vm in vms} == {8}


async def test_missing_vms_are_soft_deleted_never_dropped(client, source, service_header, session):
    await client.post(
        "/internal/inventory",
        json=batch(source.id, vm_ids=("v1", "v2", "v3")),
        headers=service_header,
    )

    result = (
        await client.post(
            "/internal/inventory", json=batch(source.id, vm_ids=("v1",)), headers=service_header
        )
    ).json()

    assert result["soft_deleted_vms"] == 2
    # Still present as rows - the history and its metrics stay queryable.
    assert (await session.execute(select(func.count(VM.id)))).scalar_one() == 3
    deleted = (
        (await session.execute(select(VM.external_id).where(VM.is_deleted.is_(True))))
        .scalars()
        .all()
    )
    assert set(deleted) == {"v2", "v3"}


async def test_a_vm_that_returns_is_undeleted(client, source, service_header, session):
    await client.post(
        "/internal/inventory", json=batch(source.id, vm_ids=("v1", "v2")), headers=service_header
    )
    await client.post(
        "/internal/inventory", json=batch(source.id, vm_ids=("v1",)), headers=service_header
    )
    await client.post(
        "/internal/inventory", json=batch(source.id, vm_ids=("v1", "v2")), headers=service_header
    )

    assert (
        await session.execute(select(func.count(VM.id)).where(VM.is_deleted.is_(True)))
    ).scalar_one() == 0


async def test_an_empty_batch_does_not_wipe_inventory(client, source, service_header, session):
    """A failed poll must not look like "the hypervisor is empty now"."""
    await client.post("/internal/inventory", json=batch(source.id), headers=service_header)

    empty = batch(source.id, vm_ids=())
    await client.post("/internal/inventory", json=empty, headers=service_header)

    assert (
        await session.execute(select(func.count(VM.id)).where(VM.is_deleted.is_(False)))
    ).scalar_one() == 2


async def test_ingest_marks_the_source_ok(client, source, service_header, session):
    collected = datetime.now(UTC).replace(microsecond=0)
    await client.post(
        "/internal/inventory", json=batch(source.id, collected_at=collected), headers=service_header
    )

    from app.db.models import HypervisorSource

    row = (
        await session.execute(select(HypervisorSource).where(HypervisorSource.id == source.id))
    ).scalar_one()
    await session.refresh(row)
    assert str(row.last_status) == "ok"
    assert row.last_inventory_at is not None


async def test_status_endpoint_records_a_failure(client, source, service_header, session):
    response = await client.post(
        f"/internal/sources/{source.id}/status",
        json={"status": "failed", "error": "connection refused"},
        headers=service_header,
    )
    assert response.status_code == 200

    from app.db.models import HypervisorSource

    row = (
        await session.execute(select(HypervisorSource).where(HypervisorSource.id == source.id))
    ).scalar_one()
    await session.refresh(row)
    assert str(row.last_status) == "failed"
    assert row.last_error == "connection refused"


async def test_internal_sources_returns_decrypted_credentials(client, source, service_header):
    response = await client.get("/internal/sources", headers=service_header)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    # Decryption happens here and nowhere else.
    assert body[0]["password"] == "pass"
    assert body[0]["endpoint"] == "vcsim:8989"


async def test_disabled_sources_are_not_handed_to_the_collector(
    client, source, service_header, session
):
    from sqlalchemy import update

    from app.db.models import HypervisorSource

    await session.execute(
        update(HypervisorSource).where(HypervisorSource.id == source.id).values(is_enabled=False)
    )
    await session.commit()

    assert (await client.get("/internal/sources", headers=service_header)).json() == []


async def test_metrics_ingest_writes_to_the_timeseries_collection(
    client, source, service_header, mongo
):
    now = datetime.now(UTC)
    points = [
        {
            "entity_type": "vm",
            "entity_external_id": "v1",
            "ts": (now - timedelta(minutes=5 * i)).isoformat(),
            "cpu_pct": 10.0 + i,
            "mem_pct": 50.0,
            "disk_iops": 100.0,
            "disk_latency_ms": 2.0,
            "net_kbps": 500.0,
        }
        for i in range(10)
    ]

    response = await client.post(
        "/internal/metrics",
        json={"source_id": str(source.id), "points": points},
        headers=service_header,
    )

    assert response.status_code == 200
    assert response.json()["points"] == 10
    assert await mongo["metrics"].count_documents({"meta.entity_external_id": "v1"}) == 10


async def test_metrics_ingest_tolerates_an_empty_batch(client, source, service_header):
    response = await client.post(
        "/internal/metrics",
        json={"source_id": str(source.id), "points": []},
        headers=service_header,
    )
    assert response.status_code == 200
    assert response.json()["points"] == 0


@pytest.mark.parametrize("bad_state", ["POWERED_ON", "running", ""])
async def test_invalid_power_state_is_rejected(client, source, service_header, bad_state):
    payload = batch(source.id)
    payload["vms"][0]["power_state"] = bad_state
    response = await client.post("/internal/inventory", json=payload, headers=service_header)
    assert response.status_code == 422


async def test_unknown_source_id_is_a_clean_404(client, service_header):
    """A stale source id must not reach the database as a foreign-key violation."""
    response = await client.post(
        "/internal/inventory", json=batch(uuid.uuid4()), headers=service_header
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_metrics_for_an_unknown_source_is_a_clean_404(client, service_header):
    response = await client.post(
        "/internal/metrics",
        json={"source_id": str(uuid.uuid4()), "points": []},
        headers=service_header,
    )
    assert response.status_code == 404
