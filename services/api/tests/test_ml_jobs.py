"""The jobs POST /ml/run and the nightly scheduler call, end to end.

These go through real Postgres and real Mongo: the point is the glue between
the models and the stores, which is exactly what a unit test would mock away.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select

from app.db.models import VM, Anomaly, Forecast, Recommendation
from app.db.models.enums import RecommendationStatus
from app.services import metrics_service
from ml import anomaly, forecast, jobs
from ml.data import load_daily, load_hourly_windows, pivot_metric

DAY = timedelta(days=1)


async def _inventory(client, source, service_header, vms: list[dict]):
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
                    "cpu_cores": 32,
                    "memory_mb": 131072,
                    "power_state": "on",
                }
            ],
            "vms": [{**vm, "host_external_id": "h1"} for vm in vms],
            "datastores": [
                {"external_id": "d1", "name": "LocalDS_0", "capacity_gb": 1000.0, "free_gb": 150.0}
            ],
        },
        headers=service_header,
    )


def _vm(external_id: str, *, vcpus: int = 8, memory_mb: int = 16384, power: str = "on") -> dict:
    return {
        "external_id": external_id,
        "name": external_id,
        "vcpus": vcpus,
        "memory_mb": memory_mb,
        "provisioned_storage_gb": 80.0,
        "guest_os": "Ubuntu 22.04",
        "power_state": power,
    }


async def _seed_daily(
    mongo,
    external_id: str,
    *,
    days: int,
    cpu: float,
    mem: float,
    net: float = 500.0,
    slope: float = 0.0,
):
    """Write metrics_daily rows directly - the rollup is tested separately."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    docs = []
    for i in range(days):
        date = today - (days - 1 - i) * DAY
        value = cpu + slope * i
        docs.append(
            {
                "entity_type": "vm",
                "entity_external_id": external_id,
                "date": date,
                "samples": 288,
                "cpu_pct_mean": value * 0.8,
                "cpu_pct_p95": value,
                "cpu_pct_max": value + 2,
                "mem_pct_mean": mem * 0.9,
                "mem_pct_p95": mem,
                "mem_pct_max": mem + 2,
                "disk_iops_mean": 120.0,
                "disk_iops_max": 200.0,
                "disk_latency_ms_mean": 2.0,
                "disk_latency_ms_max": 3.0,
                "disk_latency_ms_p95": 2.5,
                "net_kbps_mean": net,
                "net_kbps_max": net * 1.2,
            }
        )
    await mongo["metrics_daily"].insert_many(docs)


async def _seed_raw_hourly(mongo, source_id, external_id: str, *, days: int, spike_hours=()):
    """Raw 5-minute points, which is what the anomaly features are built from."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - days * DAY
    docs = []
    total_hours = days * 24
    for hour in range(total_hours):
        ts_hour = start + timedelta(hours=hour)
        spiking = hour in spike_hours
        for step in range(12):
            ts = ts_hour + timedelta(minutes=5 * step)
            diurnal = 8 * math.sin((ts.hour - 8) / 24 * 2 * math.pi)
            weekend = 0.6 if ts.weekday() >= 5 else 1.0
            cpu = (30 + diurnal) * weekend
            latency = 2.0
            if spiking:
                cpu = min(100.0, cpu + 55)
                latency = 20.0
            docs.append(
                {
                    "ts": ts,
                    "meta": {
                        "source_id": str(source_id),
                        "entity_type": "vm",
                        "entity_external_id": external_id,
                    },
                    "cpu_pct": cpu,
                    "mem_pct": 50.0,
                    "disk_iops": 100.0,
                    "disk_latency_ms": latency,
                    "net_kbps": 500.0,
                }
            )
    await mongo["metrics"].insert_many(docs, ordered=False)


# --- right-sizing ---------------------------------------------------------


async def test_rightsizing_writes_the_expected_recommendation_per_profile(
    client, source, service_header, session, mongo
):
    await _inventory(
        client,
        source,
        service_header,
        [_vm("idle-vm"), _vm("over-vm"), _vm("hot-vm"), _vm("off-vm", power="off")],
    )
    for external_id, cpu, mem, net in (
        ("idle-vm", 2.0, 15.0, 3.0),
        ("over-vm", 14.0, 22.0, 500.0),
        ("hot-vm", 93.0, 95.0, 500.0),
        ("off-vm", 1.0, 5.0, 1.0),
    ):
        await _seed_daily(mongo, external_id, days=32, cpu=cpu, mem=mem, net=net)

    result = await jobs.run_rightsizing(session, mongo, days=30)

    assert result.name == "rightsizing"
    assert result.details["vms_evaluated"] == 4

    rows = (await session.execute(select(Recommendation, VM).join(VM))).all()
    by_vm: dict[str, set[str]] = {}
    for rec, vm in rows:
        by_vm.setdefault(vm.external_id, set()).add(str(rec.kind))

    assert by_vm["idle-vm"] == {"idle"}
    assert "downsize_cpu" in by_vm["over-vm"]
    assert "upsize_cpu" in by_vm["hot-vm"]
    # Powered-off VMs are skipped entirely (spec 9.4).
    assert "off-vm" not in by_vm


async def test_rerunning_rightsizing_does_not_duplicate(
    client, source, service_header, session, mongo
):
    await _inventory(client, source, service_header, [_vm("idle-vm")])
    await _seed_daily(mongo, "idle-vm", days=32, cpu=2.0, mem=12.0, net=2.0)

    first = await jobs.run_rightsizing(session, mongo, days=30)
    second = await jobs.run_rightsizing(session, mongo, days=30)

    assert first.details["created"] == 1
    assert second.details["created"] == 0
    assert second.details["updated"] == 1
    assert (await session.execute(select(func.count(Recommendation.id)))).scalar_one() == 1


async def test_a_vm_that_stops_matching_has_its_recommendation_closed(
    client, source, service_header, session, mongo
):
    await _inventory(client, source, service_header, [_vm("swing-vm")])
    await _seed_daily(mongo, "swing-vm", days=32, cpu=2.0, mem=12.0, net=2.0)
    await jobs.run_rightsizing(session, mongo, days=30)

    # It wakes up: no longer idle.
    await mongo["metrics_daily"].delete_many({"entity_external_id": "swing-vm"})
    await _seed_daily(mongo, "swing-vm", days=32, cpu=55.0, mem=60.0, net=900.0)
    result = await jobs.run_rightsizing(session, mongo, days=30)

    assert result.details["closed"] == 1
    open_count = (
        await session.execute(
            select(func.count(Recommendation.id)).where(
                Recommendation.status == RecommendationStatus.OPEN
            )
        )
    ).scalar_one()
    assert open_count == 0


async def test_rightsizing_without_metrics_is_a_clean_skip(session, mongo):
    result = await jobs.run_rightsizing(session, mongo, days=30)
    assert result.details == {"skipped": "no daily metrics"}


# --- forecasting ----------------------------------------------------------


async def test_forecasts_are_written_for_both_models(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("grow-vm"), _vm("flat-vm")])
    await _seed_daily(mongo, "grow-vm", days=150, cpu=30.0, mem=50.0, slope=0.35)
    await _seed_daily(mongo, "flat-vm", days=150, cpu=40.0, mem=50.0)

    # Train and store an LSTM so both models are available.
    daily = await load_daily(mongo, entity_type="vm", days=145)
    wide = pivot_metric(daily, "cpu_pct_p95")
    forecast.save(forecast.train_lstm(wide, max_epochs=5, patience=2), version=1)

    result = await jobs.run_forecasts(session, mongo, days=145)

    assert result.details["forecasts_written"] > 0
    models = (await session.execute(select(Forecast.model).distinct())).scalars().all()
    assert {str(m) for m in models} == {"lstm", "naive"}
    metrics = (await session.execute(select(Forecast.metric).distinct())).scalars().all()
    assert set(metrics) >= {"cpu_pct_p95", "mem_pct_p95", "used_pct"}


async def test_forecasts_ship_the_baseline_when_no_lstm_exists(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    """The documented degraded mode: no artifact still produces a forecast."""
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("grow-vm")])
    await _seed_daily(mongo, "grow-vm", days=60, cpu=30.0, mem=50.0, slope=0.4)

    result = await jobs.run_forecasts(session, mongo, days=55)

    assert result.details["forecasts_written"] > 0
    models = (await session.execute(select(Forecast.model).distinct())).scalars().all()
    assert {str(m) for m in models} == {"naive"}


async def test_cluster_and_datastore_forecasts_are_produced(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    from app.config import settings
    from app.db.models.enums import EntityType

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("a"), _vm("b")])
    await _seed_daily(mongo, "a", days=60, cpu=40.0, mem=50.0, slope=0.2)
    await _seed_daily(mongo, "b", days=60, cpu=50.0, mem=55.0, slope=0.2)

    await jobs.run_forecasts(session, mongo, days=55)

    types = (await session.execute(select(Forecast.entity_type).distinct())).scalars().all()
    assert {str(t) for t in types} >= {
        str(EntityType.VM),
        str(EntityType.CLUSTER),
        str(EntityType.DATASTORE),
    }


async def test_days_to_threshold_is_set_when_the_series_crosses(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("busy-vm")])
    # Already above the 85% threshold, so the naive forecast crosses on day one.
    await _seed_daily(mongo, "busy-vm", days=60, cpu=90.0, mem=50.0)

    await jobs.run_forecasts(session, mongo, days=55)

    row = (
        await session.execute(select(Forecast).where(Forecast.metric == "cpu_pct_p95").limit(1))
    ).scalar_one()
    assert row.days_to_threshold == 1


async def test_forecasts_without_metrics_is_a_clean_skip(session, mongo):
    result = await jobs.run_forecasts(session, mongo, days=55)
    assert result.details == {"skipped": "no daily metrics"}


# --- anomaly detection ----------------------------------------------------


async def test_anomaly_detection_without_a_model_is_skipped(session, mongo, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    result = await jobs.run_anomaly_detection(session, mongo, days=1)
    assert result.details["skipped"] == "no trained model"
    assert result.details["created"] == 0


async def test_anomaly_detection_creates_rows_for_known_vms(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("spiky-vm")])
    # 40 days so the same-hour baseline has a full warm-up behind it.
    await _seed_raw_hourly(
        mongo, source.id, "spiky-vm", days=40, spike_hours={40 * 24 - 30, 40 * 24 - 29}
    )

    windows = await load_hourly_windows(mongo, days=10)
    assert not windows.empty
    anomaly.save(anomaly.train(windows, flag_rate=0.01))

    result = await jobs.run_anomaly_detection(session, mongo, days=10)

    assert result.details["windows"] > 0
    assert result.details["created"] >= 1
    rows = (await session.execute(select(Anomaly))).scalars().all()
    assert rows
    assert rows[0].features, "the features that fired the alert are recorded as evidence"
    assert str(rows[0].status) == "open"


async def test_rerunning_detection_updates_rather_than_duplicating(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("spiky-vm")])
    await _seed_raw_hourly(
        mongo, source.id, "spiky-vm", days=40, spike_hours={40 * 24 - 30, 40 * 24 - 29}
    )
    windows = await load_hourly_windows(mongo, days=10)
    anomaly.save(anomaly.train(windows, flag_rate=0.01))

    first = await jobs.run_anomaly_detection(session, mongo, days=10)
    before = (await session.execute(select(func.count(Anomaly.id)))).scalar_one()
    second = await jobs.run_anomaly_detection(session, mongo, days=10)
    after = (await session.execute(select(func.count(Anomaly.id)))).scalar_one()

    assert first.details["created"] >= 1
    assert second.details["created"] == 0
    assert before == after


async def test_detection_ignores_entities_that_are_not_in_inventory(
    session, mongo, source, tmp_path, monkeypatch
):
    """Metrics can outlive a VM; an anomaly needs something to point at."""
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _seed_raw_hourly(mongo, source.id, "ghost-vm", days=40, spike_hours={40 * 24 - 30})
    windows = await load_hourly_windows(mongo, days=10)
    anomaly.save(anomaly.train(windows, flag_rate=0.05))

    result = await jobs.run_anomaly_detection(session, mongo, days=10)

    assert result.details["created"] == 0
    assert (await session.execute(select(func.count(Anomaly.id)))).scalar_one() == 0


# --- run_all --------------------------------------------------------------


async def test_run_all_reports_every_job(
    client, source, service_header, session, mongo, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    await _inventory(client, source, service_header, [_vm("idle-vm")])
    await _seed_daily(mongo, "idle-vm", days=60, cpu=2.0, mem=12.0, net=2.0)

    results = await jobs.run_all(session, mongo, anomaly_days=1, history_days=55)

    assert set(results) == {"anomaly", "forecast", "rightsizing"}
    for payload in results.values():
        assert "duration_seconds" in payload
        assert payload["started_at"] <= payload["finished_at"]


async def test_powered_on_count_and_cluster_names(client, source, service_header, session):
    await _inventory(
        client, source, service_header, [_vm("on-1"), _vm("on-2"), _vm("off-1", power="off")]
    )

    assert await jobs.powered_on_vm_count(session) == 2
    assert set((await jobs.cluster_names(session)).values()) == {"DC0_C0"}


# --- rollup integration ---------------------------------------------------


async def test_rollup_feeds_the_daily_loader(client, source, service_header, mongo):
    await _seed_raw_hourly(mongo, source.id, "roll-vm", days=10)

    await metrics_service.rollup_daily(mongo, days=11)
    frame = await load_daily(mongo, entity_type="vm", days=8)

    assert not frame.empty
    assert set(frame["entity_external_id"]) == {"roll-vm"}
    assert frame["cpu_pct_p95"].notna().all()


async def test_sparse_entities_are_dropped_by_the_loader(mongo):
    """More than 10% of days missing and the entity is excluded (spec 9.1)."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    await mongo["metrics_daily"].insert_many(
        [
            {
                "entity_type": "vm",
                "entity_external_id": "sparse-vm",
                "date": today - i * DAY,
                "samples": 288,
                "cpu_pct_mean": 10.0,
                "cpu_pct_p95": 12.0,
            }
            for i in (0, 1, 2)
        ]
    )

    frame = await load_daily(mongo, entity_type="vm", days=60)
    assert frame.empty


async def test_short_gaps_are_forward_filled(mongo):
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    days = 30
    docs = []
    for i in range(days):
        if i in (10, 11):  # a two-day hole, inside the fill limit
            continue
        docs.append(
            {
                "entity_type": "vm",
                "entity_external_id": "gappy-vm",
                "date": today - (days - 1 - i) * DAY,
                "samples": 288,
                "cpu_pct_mean": 20.0,
                "cpu_pct_p95": 25.0,
                "mem_pct_mean": 40.0,
                "mem_pct_p95": 45.0,
            }
        )
    await mongo["metrics_daily"].insert_many(docs)

    frame = await load_daily(mongo, entity_type="vm", days=days)

    assert not frame.empty
    assert frame["cpu_pct_p95"].notna().all()


def test_job_result_serialises_its_timings():
    started = datetime(2026, 1, 1, tzinfo=UTC)
    result = jobs.JobResult("demo", started, started + timedelta(seconds=2.5), {"created": 1})
    payload = result.to_dict()
    assert payload["duration_seconds"] == 2.5
    assert payload["created"] == 1
    assert payload["name"] == "demo"


def test_forecast_metrics_are_the_documented_pair():
    assert jobs.FORECAST_METRICS == ("cpu_pct_p95", "mem_pct_p95")


def test_pivot_metric_on_an_empty_frame_is_empty():
    assert pivot_metric(pd.DataFrame(), "cpu_pct_p95").empty
