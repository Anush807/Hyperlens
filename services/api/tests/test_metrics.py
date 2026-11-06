"""Metric reads, resolutions and the daily rollup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import metrics_service
from tests.conftest import auth_header


async def _vm(client, source, service_header, external_id: str = "v1") -> dict:
    await client.post(
        "/internal/inventory",
        json={
            "source_id": str(source.id),
            "collected_at": datetime.now(UTC).isoformat(),
            "vms": [
                {
                    "external_id": external_id,
                    "name": "metric-vm",
                    "vcpus": 4,
                    "memory_mb": 8192,
                    "provisioned_storage_gb": 40.0,
                    "power_state": "on",
                }
            ],
        },
        headers=service_header,
    )
    page = await client.get("/vms", headers={"X-Service-Token": "x", **service_header})
    return page.json() if page.status_code == 200 else {}


async def _seed_points(
    client, source, service_header, *, hours: int, external_id="v1", step_minutes=5
):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    points = []
    steps = int(hours * 60 / step_minutes)
    for i in range(steps):
        ts = now - timedelta(minutes=step_minutes * i)
        points.append(
            {
                "entity_type": "vm",
                "entity_external_id": external_id,
                "ts": ts.isoformat(),
                # A ramp, so means and p95 are predictable.
                "cpu_pct": float(i % 100),
                "mem_pct": 50.0,
                "disk_iops": 100.0,
                "disk_latency_ms": 2.0,
                "net_kbps": 500.0,
            }
        )
    response = await client.post(
        "/internal/metrics",
        json={"source_id": str(source.id), "points": points},
        headers=service_header,
    )
    assert response.status_code == 200
    return len(points)


async def test_metrics_require_authentication(client, viewer, source, service_header):
    await _vm(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]
    assert (await client.get(f"/metrics/vm/{vm_id}")).status_code == 401


async def test_unknown_entity_is_404(client, viewer):
    response = await client.get(
        "/metrics/vm/11111111-1111-1111-1111-111111111111", headers=auth_header(viewer)
    )
    assert response.status_code == 404


async def test_raw_resolution_returns_every_point(client, viewer, source, service_header, mongo):
    await _vm(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]
    count = await _seed_points(client, source, service_header, hours=6)

    response = await client.get(
        f"/metrics/vm/{vm_id}", params={"resolution": "raw"}, headers=auth_header(viewer)
    )

    body = response.json()
    assert body["resolution"] == "raw"
    assert body["entity_name"] == "metric-vm"
    assert len(body["points"]) == count
    assert body["points"][0]["ts"] < body["points"][-1]["ts"]


async def test_hourly_resolution_aggregates_into_buckets(
    client, viewer, source, service_header, mongo
):
    await _vm(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]
    await _seed_points(client, source, service_header, hours=6)

    body = (
        await client.get(
            f"/metrics/vm/{vm_id}", params={"resolution": "hourly"}, headers=auth_header(viewer)
        )
    ).json()

    assert 6 <= len(body["points"]) <= 8
    row = body["points"][-1]
    assert row["cpu_pct_p95"] is not None
    assert row["cpu_pct_p95"] >= row["cpu_pct"]


async def test_raw_requests_over_a_week_are_clamped(client, viewer, source, service_header, mongo):
    await _vm(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]

    body = (
        await client.get(
            f"/metrics/vm/{vm_id}",
            params={
                "resolution": "raw",
                "from": (datetime.now(UTC) - timedelta(days=90)).isoformat(),
            },
            headers=auth_header(viewer),
        )
    ).json()

    span = datetime.fromisoformat(body["to_ts"]) - datetime.fromisoformat(body["from_ts"])
    assert span <= timedelta(days=7, seconds=1)


async def test_an_empty_window_returns_an_empty_series(
    client, viewer, source, service_header, mongo
):
    await _vm(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]

    body = (
        await client.get(
            f"/metrics/vm/{vm_id}", params={"resolution": "hourly"}, headers=auth_header(viewer)
        )
    ).json()
    assert body["points"] == []


async def test_rollup_materialises_daily_rows(client, source, service_header, mongo):
    await _seed_points(client, source, service_header, hours=48)

    await metrics_service.rollup_daily(mongo, days=3)

    rows = await mongo["metrics_daily"].find({"entity_external_id": "v1"}).to_list(length=10)
    assert len(rows) >= 2
    row = rows[0]
    assert row["cpu_pct_mean"] is not None
    assert row["cpu_pct_p95"] >= row["cpu_pct_mean"]
    assert row["samples"] > 0


async def test_rollup_is_idempotent(client, source, service_header, mongo):
    await _seed_points(client, source, service_header, hours=24)

    await metrics_service.rollup_daily(mongo, days=2)
    first = await mongo["metrics_daily"].count_documents({})
    await metrics_service.rollup_daily(mongo, days=2)
    second = await mongo["metrics_daily"].count_documents({})

    assert first == second


async def test_daily_resolution_reads_the_rolled_up_collection(
    client, viewer, source, service_header, mongo
):
    await _vm(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]
    await _seed_points(client, source, service_header, hours=72)
    await metrics_service.rollup_daily(mongo, days=4)

    body = (
        await client.get(
            f"/metrics/vm/{vm_id}",
            params={
                "resolution": "daily",
                "from": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
            },
            headers=auth_header(viewer),
        )
    ).json()

    assert body["resolution"] == "daily"
    assert len(body["points"]) >= 3
    assert all(p["cpu_pct_p95"] is not None for p in body["points"])


async def test_metric_stats_summarise_the_window(client, source, service_header, mongo):
    await _seed_points(client, source, service_header, hours=72)
    await metrics_service.rollup_daily(mongo, days=4)

    stats = await metrics_service.metric_stats(
        mongo, entity_type="vm", external_id="v1", metric="cpu_pct", days=5
    )

    assert stats.samples >= 3
    assert stats.mean > 0
    assert stats.p95 >= stats.mean
    assert stats.max >= stats.p95


async def test_metric_stats_on_no_data_is_all_zeroes(mongo):
    stats = await metrics_service.metric_stats(
        mongo, entity_type="vm", external_id="nope", metric="cpu_pct", days=7
    )
    assert (stats.mean, stats.p95, stats.max, stats.samples) == (0.0, 0.0, 0.0, 0)


async def test_metric_stats_rejects_an_unknown_metric(mongo):
    with pytest.raises(ValueError, match="unknown metric"):
        await metrics_service.metric_stats(
            mongo, entity_type="vm", external_id="v1", metric="made_up", days=7
        )


def test_trend_slope_is_positive_for_a_rising_series():
    assert metrics_service._slope([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
    assert metrics_service._slope([4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert metrics_service._slope([5.0]) == 0.0


def test_p95_is_nearest_rank():
    assert metrics_service._p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 10
    assert metrics_service._p95([]) == 0.0
