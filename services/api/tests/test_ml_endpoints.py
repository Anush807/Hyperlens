"""Anomaly, forecast, capacity, recommendation and /ml endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models.enums import (
    EntityType,
    ForecastModel,
    RecommendationKind,
)
from app.services import ml_service
from tests.conftest import auth_header


async def _make_vm(client, source, service_header, external_id="v1", name="vm-1"):
    await client.post(
        "/internal/inventory",
        json={
            "source_id": str(source.id),
            "collected_at": datetime.now(UTC).isoformat(),
            "clusters": [{"external_id": "c1", "name": "DC0_C0"}],
            "hosts": [
                {
                    "external_id": "h1",
                    "cluster_external_id": "c1",
                    "name": "esx-01",
                    "cpu_cores": 16,
                    "memory_mb": 65536,
                    "power_state": "on",
                }
            ],
            "vms": [
                {
                    "external_id": external_id,
                    "host_external_id": "h1",
                    "name": name,
                    "vcpus": 8,
                    "memory_mb": 16384,
                    "provisioned_storage_gb": 100.0,
                    "power_state": "on",
                }
            ],
            "datastores": [
                {"external_id": "d1", "name": "LocalDS_0", "capacity_gb": 1000.0, "free_gb": 100.0}
            ],
        },
        headers=service_header,
    )


async def _vm_id(client, user):
    return (await client.get("/vms", headers=auth_header(user))).json()["items"][0]["id"]


async def _add_anomaly(session, vm_id, *, hours_ago: int = 2, score: float = -0.3):
    start = datetime.now(UTC) - timedelta(hours=hours_ago)
    row, created = await ml_service.upsert_anomaly(
        session,
        entity_type=EntityType.VM,
        entity_id=uuid.UUID(vm_id),
        window_start=start,
        window_end=start + timedelta(hours=1),
        score=score,
        features={"cpu_mean": 93.4, "lat_mean": 18.2},
    )
    await session.commit()
    return row, created


# --- anomalies ------------------------------------------------------------


async def test_anomaly_list_is_paginated_and_named(client, viewer, source, service_header, session):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_anomaly(session, vm_id)

    body = (await client.get("/anomalies", headers=auth_header(viewer))).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["entity_name"] == "vm-1"
    assert item["status"] == "open"
    assert item["features"]["cpu_mean"] == 93.4


async def test_anomaly_filters(client, viewer, source, service_header, session):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_anomaly(session, vm_id, hours_ago=2)

    assert (
        await client.get("/anomalies", params={"status": "resolved"}, headers=auth_header(viewer))
    ).json()["total"] == 0
    assert (await client.get("/anomalies", params={"days": 1}, headers=auth_header(viewer))).json()[
        "total"
    ] == 1
    assert (
        await client.get("/anomalies", params={"entity_id": vm_id}, headers=auth_header(viewer))
    ).json()["total"] == 1


async def test_overlapping_windows_merge_instead_of_duplicating(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)

    _, first = await _add_anomaly(session, vm_id, hours_ago=3, score=-0.2)
    _, second = await _add_anomaly(session, vm_id, hours_ago=3, score=-0.4)

    assert first is True
    assert second is False, "an overlapping window should extend the existing row"

    body = (await client.get("/anomalies", headers=auth_header(viewer))).json()
    assert body["total"] == 1
    # The more anomalous (lower) score wins.
    assert body["items"][0]["score"] == -0.4


async def test_distant_windows_create_separate_anomalies(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_anomaly(session, vm_id, hours_ago=2)
    await _add_anomaly(session, vm_id, hours_ago=48)

    assert (await client.get("/anomalies", headers=auth_header(viewer))).json()["total"] == 2


async def test_operator_can_acknowledge_but_viewer_cannot(
    client, viewer, operator, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    row, _ = await _add_anomaly(session, vm_id)

    assert (
        await client.patch(
            f"/anomalies/{row.id}", json={"status": "acknowledged"}, headers=auth_header(viewer)
        )
    ).status_code == 403

    response = await client.patch(
        f"/anomalies/{row.id}", json={"status": "acknowledged"}, headers=auth_header(operator)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"


async def test_patching_an_unknown_anomaly_is_404(client, operator):
    response = await client.patch(
        "/anomalies/11111111-1111-1111-1111-111111111111",
        json={"status": "resolved"},
        headers=auth_header(operator),
    )
    assert response.status_code == 404


async def test_acknowledging_writes_an_audit_row(
    client, operator, viewer, source, service_header, session
):
    from sqlalchemy import select

    from app.db.models import AuditLog

    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    row, _ = await _add_anomaly(session, vm_id)

    await client.patch(
        f"/anomalies/{row.id}", json={"status": "resolved"}, headers=auth_header(operator)
    )

    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "anomaly.resolved" in actions


# --- forecasts and capacity ----------------------------------------------


async def _add_forecast(session, entity_type, entity_id, *, model, metric="cpu_pct_p95", days=12):
    today = datetime.now(UTC).date()
    points = [
        {
            "date": (today + timedelta(days=i + 1)).isoformat(),
            "value": 70.0 + i,
            "lower": 68.0 + i,
            "upper": 72.0 + i,
        }
        for i in range(30)
    ]
    await ml_service.save_forecast(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        metric=metric,
        model=model,
        horizon_days=30,
        points=points,
        days_to_threshold=days,
    )
    await session.commit()


async def test_forecasts_return_the_latest_per_model(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = uuid.UUID(await _vm_id(client, viewer))
    await _add_forecast(session, EntityType.VM, vm_id, model=ForecastModel.NAIVE)
    await _add_forecast(session, EntityType.VM, vm_id, model=ForecastModel.LSTM)

    body = (await client.get(f"/forecasts/vm/{vm_id}", headers=auth_header(viewer))).json()

    assert {f["model"] for f in body["forecasts"]} == {"naive", "lstm"}
    assert body["threshold_pct"] == 85.0
    assert body["preferred_model"] in {"naive", "lstm"}
    assert len(body["forecasts"][0]["points"]) == 30


async def test_regenerating_a_forecast_supersedes_the_old_one(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = uuid.UUID(await _vm_id(client, viewer))
    await _add_forecast(session, EntityType.VM, vm_id, model=ForecastModel.NAIVE, days=20)
    await _add_forecast(session, EntityType.VM, vm_id, model=ForecastModel.NAIVE, days=5)

    body = (await client.get(f"/forecasts/vm/{vm_id}", headers=auth_header(viewer))).json()
    assert len(body["forecasts"]) == 1
    assert body["forecasts"][0]["days_to_threshold"] == 5


async def test_forecasts_for_an_unknown_entity_are_404(client, viewer):
    response = await client.get(
        "/forecasts/vm/11111111-1111-1111-1111-111111111111", headers=auth_header(viewer)
    )
    assert response.status_code == 404


async def test_cluster_capacity_reports_headroom_and_the_preferred_model(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    cluster_id = uuid.UUID(
        (await client.get("/clusters", headers=auth_header(viewer))).json()["items"][0]["id"]
    )
    await _add_forecast(session, EntityType.CLUSTER, cluster_id, model=ForecastModel.NAIVE, days=9)

    body = (
        await client.get(f"/capacity/clusters/{cluster_id}", headers=auth_header(viewer))
    ).json()

    assert body["entity_name"] == "DC0_C0"
    assert body["metrics"]["cpu_pct_p95"] == 9
    assert body["headroom"]["physical_cores"] == 16
    assert body["headroom"]["allocated_vcpus"] == 8
    assert body["headroom"]["vcpu_overcommit_ratio"] == 0.5


async def test_datastore_capacity_reports_usage(client, viewer, source, service_header):
    await _make_vm(client, source, service_header)
    datastore = (await client.get("/datastores", headers=auth_header(viewer))).json()["items"][0]

    body = (
        await client.get(f"/capacity/datastores/{datastore['id']}", headers=auth_header(viewer))
    ).json()

    assert body["entity_name"] == "LocalDS_0"
    assert body["headroom"]["used_pct"] == 90.0


# --- recommendations ------------------------------------------------------


async def _add_recommendation(session, vm_id, kind, current, suggested):
    row, created = await ml_service.upsert_recommendation(
        session,
        vm_id=uuid.UUID(vm_id),
        kind=kind,
        current_value=current,
        suggested_value=suggested,
        evidence={"rule": "test", "window_days": 14, "cpu_pct_p95": 12.0},
    )
    await session.commit()
    return row, created


async def test_recommendations_report_reclaimable_totals(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_CPU, 8, 3)
    await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_MEM, 16384, 8192)

    body = (await client.get("/recommendations", headers=auth_header(viewer))).json()

    assert body["total"] == 2
    assert body["total_reclaimable_vcpus"] == 5
    assert body["total_reclaimable_memory_mb"] == 8192
    assert {item["kind"] for item in body["items"]} == {"downsize_cpu", "downsize_mem"}
    assert body["items"][0]["vm_name"] == "vm-1"
    assert body["items"][0]["cluster_name"] == "DC0_C0"


async def test_an_idle_recommendation_reclaims_the_whole_vm(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_recommendation(session, vm_id, RecommendationKind.IDLE, 8, 0)

    body = (await client.get("/recommendations", headers=auth_header(viewer))).json()
    assert body["total_reclaimable_vcpus"] == 8
    assert body["total_reclaimable_memory_mb"] == 16384


async def test_upsize_recommendations_reclaim_nothing(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_recommendation(session, vm_id, RecommendationKind.UPSIZE_CPU, 8, 12)

    body = (await client.get("/recommendations", headers=auth_header(viewer))).json()
    assert body["total_reclaimable_vcpus"] == 0


async def test_rerunning_updates_rather_than_duplicating(
    client, viewer, source, service_header, session
):
    """Spec 9.4: a second pass must not stack a second open row."""
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    _, first = await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_CPU, 8, 3)
    _, second = await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_CPU, 8, 2)

    assert first is True
    assert second is False

    body = (await client.get("/recommendations", headers=auth_header(viewer))).json()
    assert body["total"] == 1
    assert body["items"][0]["suggested_value"] == 2


async def test_dismissed_recommendations_are_not_reopened(
    client, operator, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    row, _ = await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_CPU, 8, 3)

    await client.patch(
        f"/recommendations/{row.id}", json={"status": "dismissed"}, headers=auth_header(operator)
    )
    _, created = await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_CPU, 8, 3)

    assert created is True, "a dismissed row stays dismissed; a fresh one is opened"
    open_rows = (
        await client.get("/recommendations", params={"status": "open"}, headers=auth_header(viewer))
    ).json()
    assert open_rows["total"] == 1


async def test_viewer_cannot_dismiss(client, viewer, source, service_header, session):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    row, _ = await _add_recommendation(session, vm_id, RecommendationKind.IDLE, 8, 0)

    response = await client.patch(
        f"/recommendations/{row.id}", json={"status": "dismissed"}, headers=auth_header(viewer)
    )
    assert response.status_code == 403


async def test_stale_recommendations_are_closed(client, viewer, source, service_header, session):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_recommendation(session, vm_id, RecommendationKind.DOWNSIZE_CPU, 8, 3)

    closed = await ml_service.close_stale_recommendations(
        session, keep=set(), generated_before=datetime.now(UTC) + timedelta(seconds=1)
    )
    await session.commit()

    assert closed == 1
    body = (
        await client.get("/recommendations", params={"status": "open"}, headers=auth_header(viewer))
    ).json()
    assert body["total"] == 0


async def test_recommendations_for_deleted_vms_are_hidden(
    client, viewer, source, service_header, session
):
    await _make_vm(client, source, service_header)
    vm_id = await _vm_id(client, viewer)
    await _add_recommendation(session, vm_id, RecommendationKind.IDLE, 8, 0)

    # The VM disappears from the next full inventory batch.
    await client.post(
        "/internal/inventory",
        json={
            "source_id": str(source.id),
            "collected_at": datetime.now(UTC).isoformat(),
            "vms": [
                {
                    "external_id": "other",
                    "name": "other-vm",
                    "vcpus": 2,
                    "memory_mb": 4096,
                    "provisioned_storage_gb": 10.0,
                    "power_state": "on",
                }
            ],
        },
        headers=service_header,
    )

    body = (await client.get("/recommendations", headers=auth_header(viewer))).json()
    assert body["total"] == 0


# --- /ml admin ------------------------------------------------------------


async def test_ml_status_is_admin_only(client, viewer, operator, admin):
    assert (await client.get("/ml/status", headers=auth_header(viewer))).status_code == 403
    assert (await client.get("/ml/status", headers=auth_header(operator))).status_code == 403

    body = (await client.get("/ml/status", headers=auth_header(admin))).json()
    assert set(body) >= {"anomaly", "forecast", "rightsizing", "artifact_dir"}
    assert body["forecast"]["preferred_model"] in {"naive", "lstm"}


async def test_ml_reload_is_admin_only_and_idempotent(client, viewer, admin):
    assert (await client.post("/ml/reload", headers=auth_header(viewer))).status_code == 403

    body = (await client.post("/ml/reload", headers=auth_header(admin))).json()
    assert set(body) == {"anomaly_model", "forecast_model", "loaded_at"}


async def test_ml_run_queues_a_background_task(client, admin, viewer, monkeypatch):
    calls: list[tuple[int, int]] = []

    async def fake_jobs(anomaly_days: int, history_days: int) -> None:
        calls.append((anomaly_days, history_days))

    from app.routes import ml_admin

    monkeypatch.setattr(ml_admin, "_run_jobs", fake_jobs)

    assert (await client.post("/ml/run", headers=auth_header(viewer))).status_code == 403

    response = await client.post(
        "/ml/run", params={"anomaly_days": 3, "history_days": 90}, headers=auth_header(admin)
    )

    assert response.status_code == 200
    assert response.json()["queued"] is True
    assert calls == [(3, 90)]


async def test_ml_run_refuses_to_overlap(client, admin):
    from app.services.ml_state import state

    state.running = True
    try:
        body = (await client.post("/ml/run", headers=auth_header(admin))).json()
        assert body["queued"] is False
    finally:
        state.running = False
