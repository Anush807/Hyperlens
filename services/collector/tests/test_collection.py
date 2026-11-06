"""Per-source failure isolation and the metric collection cycle (spec 7.7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app import collection
from app.adapters.base import AdapterError, HypervisorAdapter
from app.config import settings
from app.ingest_client import IngestError
from app.schemas import ClusterIn, DatastoreIn, HostIn, VMIn


class StubAdapter(HypervisorAdapter):
    def __init__(self, source, *, fail: bool = False):
        super().__init__(source)
        self.fail = fail
        self.closed = False

    def connect(self):
        if self.fail:
            raise AdapterError("connection refused")

    def close(self):
        self.closed = True

    def list_clusters(self):
        return [ClusterIn(external_id="c1", name="cluster-1")]

    def list_hosts(self):
        return [
            HostIn(
                external_id="h1",
                cluster_external_id="c1",
                name="host-1",
                cpu_cores=16,
                cpu_mhz_per_core=2600,
                memory_mb=65536,
                power_state="on",
            )
        ]

    def list_vms(self):
        return [
            VMIn(
                external_id="v1",
                host_external_id="h1",
                name="vm-1",
                vcpus=4,
                memory_mb=8192,
                provisioned_storage_gb=40,
                guest_os="Ubuntu",
                power_state="on",
            )
        ]

    def list_datastores(self):
        return [DatastoreIn(external_id="d1", name="ds-1", capacity_gb=1000, free_gb=400)]


class FakeClient:
    def __init__(self, sources, *, fail_ingest: bool = False):
        self.sources = sources
        self.fail_ingest = fail_ingest
        self.inventory_batches = []
        self.metric_points = []
        self.statuses = []

    def fetch_sources(self):
        return self.sources

    def post_inventory(self, batch):
        if self.fail_ingest:
            raise IngestError("ingest rejected the batch")
        self.inventory_batches.append(batch)
        return {"vms": len(batch.vms), "hosts": len(batch.hosts)}

    def post_metrics_chunked(self, source_id, points, chunk_size=None):
        self.metric_points.extend(points)
        return len(points)

    def report_status(self, source_id, status, error=None, collected_at=None):
        self.statuses.append((source_id, status, error))


@pytest.fixture(autouse=True)
def clear_cache():
    collection._entity_cache.clear()
    yield
    collection._entity_cache.clear()


def test_successful_run_reports_ok_and_caches_entities(monkeypatch, vsphere_source):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = FakeClient([vsphere_source])

    result = collection.collect_inventory(vsphere_source, client)

    assert result.ok is True
    assert result.counts["vms"] == 1
    assert client.statuses == [(vsphere_source.id, "ok", None)]
    # VM + host are cached so the metrics job does not re-walk the hypervisor.
    assert len(collection._entity_cache[vsphere_source.id]) == 2


def test_adapter_failure_is_reported_not_raised(monkeypatch, vsphere_source):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s, fail=True))
    client = FakeClient([vsphere_source])

    result = collection.collect_inventory(vsphere_source, client)

    assert result.ok is False
    assert "connection refused" in result.error
    assert client.statuses == [(vsphere_source.id, "failed", result.error)]
    assert client.inventory_batches == []


def test_one_failing_source_does_not_stop_the_others(monkeypatch, vsphere_source, nutanix_source):
    def adapter_for(source):
        return StubAdapter(source, fail=source.id == nutanix_source.id)

    monkeypatch.setattr(collection, "get_adapter", adapter_for)
    client = FakeClient([nutanix_source, vsphere_source])

    results = collection.run_inventory_cycle(client)

    by_name = {r["source"]: r for r in results}
    assert by_name["nutanix-mock"]["ok"] is False
    assert by_name["vcsim-lab"]["ok"] is True
    assert len(client.inventory_batches) == 1


def test_ingest_failure_marks_the_source_failed(monkeypatch, vsphere_source):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = FakeClient([vsphere_source], fail_ingest=True)

    result = collection.collect_inventory(vsphere_source, client)

    assert result.ok is False
    assert "ingest rejected" in result.error
    assert client.statuses[0][1] == "failed"


def test_unreachable_api_returns_an_error_row_rather_than_raising(vsphere_source):
    class DeadClient(FakeClient):
        def fetch_sources(self):
            raise IngestError("connection refused")

    results = collection.run_inventory_cycle(DeadClient([vsphere_source]))
    assert results == [{"ok": False, "error": "connection refused"}]


def test_metrics_cycle_skips_sources_with_no_cached_inventory(vsphere_source):
    client = FakeClient([vsphere_source])
    result = collection.collect_metrics(vsphere_source, client)

    assert result.ok is True
    assert result.counts == {"points": 0, "skipped": 1}
    assert client.metric_points == []


def test_metrics_cycle_generates_points_for_cached_entities(monkeypatch, vsphere_source):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = FakeClient([vsphere_source])
    collection.collect_inventory(vsphere_source, client)

    result = collection.collect_metrics(vsphere_source, client, window_seconds=3600)

    assert result.ok is True
    assert result.counts["points"] > 0
    assert {p.entity_type for p in client.metric_points} == {"vm", "host"}
    recent = datetime.now(UTC) - timedelta(hours=2)
    assert all(p.ts >= recent for p in client.metric_points)


def test_trigger_for_one_source_filters_the_rest(monkeypatch, vsphere_source, nutanix_source):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = FakeClient([vsphere_source, nutanix_source])

    results = collection.run_inventory_cycle(client, source_id=vsphere_source.id)

    assert len(results) == 1
    assert results[0]["source"] == "vcsim-lab"


def test_unknown_source_id_is_reported(vsphere_source):
    client = FakeClient([vsphere_source])
    missing = uuid.uuid4()
    results = collection.run_inventory_cycle(client, source_id=missing)
    assert results[0]["ok"] is False
    assert str(missing) in results[0]["error"]


# --- catching up after an outage -----------------------------------------


class WatermarkClient(FakeClient):
    """A FakeClient that also answers the watermark call."""

    def __init__(self, sources, watermarks=None, *, watermark_fails: bool = False):
        super().__init__(sources)
        self._watermarks = watermarks or {}
        self.watermark_fails = watermark_fails
        self.watermark_calls = 0

    def fetch_watermarks(self):
        self.watermark_calls += 1
        if self.watermark_fails:
            return {}
        return self._watermarks


def test_an_explicit_window_still_wins(now):
    """Manual triggers and tests pin the window deliberately."""
    window = collection.resolve_metric_window(now - timedelta(days=5), now, window_seconds=60)
    assert window.catching_up is False
    assert window.since == now - timedelta(seconds=60)
    assert window.until == now


def test_a_source_with_no_watermark_generates_one_interval(now):
    """A brand new source must not trigger a surprise backfill from a 60s poll."""
    window = collection.resolve_metric_window(None, now)
    assert window.catching_up is False
    assert (window.until - window.since) == timedelta(seconds=settings.poll_metrics_seconds)


def test_a_current_watermark_is_not_a_gap(now):
    window = collection.resolve_metric_window(now - timedelta(seconds=30), now)
    assert window.catching_up is False
    assert window.until == now


def test_a_few_minutes_behind_is_normal_not_a_recovery(now):
    """Points land on a 5-minute grid but the poll runs every 60s, so the
    watermark is routinely minutes old while perfectly healthy. That must not
    log a catch-up warning on every cycle."""
    window = collection.resolve_metric_window(now - timedelta(minutes=5), now)
    assert window.catching_up is False
    # Still generated from the watermark, so nothing between polls is lost.
    assert window.since > now - timedelta(minutes=5, seconds=1)
    assert window.until == now


def test_generation_starts_after_the_watermark_not_on_it(now):
    """The watermark point is already stored; re-emitting it would duplicate a
    point on every single cycle."""
    watermark = now - timedelta(hours=2)
    window = collection.resolve_metric_window(watermark, now)
    assert window.since > watermark


def test_an_outage_generates_from_the_watermark_forward(now):
    """The actual fix: the span the collector was down for gets produced."""
    watermark = now - timedelta(hours=30)
    window = collection.resolve_metric_window(watermark, now, chunk_hours=12)

    assert window.catching_up is True
    assert window.since > watermark
    assert window.gap_hours == 30.0
    # One cycle advances by at most the chunk, so the batch stays bounded.
    assert window.until <= watermark + timedelta(hours=12, seconds=1)
    assert 17.9 <= window.remaining_hours <= 18.1
    assert window.truncated is False


def test_successive_cycles_walk_the_gap_closed(now):
    """Each cycle resumes where the last one stopped, so the hole closes."""
    watermark = now - timedelta(hours=30)
    seen = []
    for _ in range(10):
        window = collection.resolve_metric_window(watermark, now, chunk_hours=12)
        seen.append((window.since, window.until))
        watermark = window.until
        if not window.catching_up:
            break

    # 30 hours at 12 per cycle: 12 + 12 + 6, then caught up.
    assert [round((u - s).total_seconds() / 3600, 1) for s, u in seen[:3]] == [12.0, 12.0, 6.0]
    assert seen[1][0] > seen[0][1] - timedelta(seconds=2), "cycles must not overlap"
    assert collection.resolve_metric_window(watermark, now).catching_up is False


def test_an_absurd_gap_is_capped_and_flagged(now):
    """Beyond the cap, automatic recovery is the wrong tool; say so."""
    window = collection.resolve_metric_window(
        now - timedelta(days=400), now, chunk_hours=12, max_catchup_days=30
    )
    assert window.truncated is True
    assert window.gap_hours == 30 * 24
    assert window.since == now - timedelta(days=30)


def test_catchup_can_be_switched_off(now, monkeypatch):
    monkeypatch.setattr(settings, "metrics_catchup_enabled", False)
    window = collection.resolve_metric_window(now - timedelta(days=5), now)
    assert window.catching_up is False
    assert (window.until - window.since) == timedelta(seconds=settings.poll_metrics_seconds)


def test_the_cycle_fetches_watermarks_once_for_all_sources(
    monkeypatch, vsphere_source, nutanix_source
):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = WatermarkClient([vsphere_source, nutanix_source])
    collection.collect_inventory(vsphere_source, client)
    collection.collect_inventory(nutanix_source, client)

    collection.run_metrics_cycle(client)

    assert client.watermark_calls == 1, "one call per cycle, not per source"


def test_an_outage_produces_far_more_points_than_a_single_interval(
    monkeypatch, vsphere_source, now
):
    """End to end through collect_metrics: a day-old watermark must ship a day."""
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = WatermarkClient([vsphere_source])
    collection.collect_inventory(vsphere_source, client)

    client.metric_points.clear()
    collection.collect_metrics(
        vsphere_source, client, watermark=datetime.now(UTC) - timedelta(hours=24)
    )
    caught_up = len(client.metric_points)

    client.metric_points.clear()
    collection.collect_metrics(vsphere_source, client, window_seconds=60)
    single_interval = len(client.metric_points)

    assert caught_up > single_interval * 50, (caught_up, single_interval)


def test_a_failed_watermark_call_degrades_to_one_interval(
    monkeypatch, vsphere_source, nutanix_source
):
    """Losing catch-up must not lose the metrics cycle."""
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = WatermarkClient([vsphere_source], watermark_fails=True)
    collection.collect_inventory(vsphere_source, client)

    results = collection.run_metrics_cycle(client)

    assert all(r["ok"] for r in results)
    assert client.watermark_calls == 1


def test_catchup_progress_is_reported_in_the_run_result(monkeypatch, vsphere_source):
    monkeypatch.setattr(collection, "get_adapter", lambda s: StubAdapter(s))
    client = WatermarkClient([vsphere_source])
    collection.collect_inventory(vsphere_source, client)

    result = collection.collect_metrics(
        vsphere_source, client, watermark=datetime.now(UTC) - timedelta(hours=30)
    )

    assert result.ok is True
    assert result.counts["catchup_gap_hours"] >= 29.0
    assert result.counts["catchup_remaining_hours"] > 0


def test_log_records_carry_their_extra_fields(caplog):
    """The formatter used to interpolate only level/logger/message, so every
    structured field this service logs was discarded. Guard against that."""
    import json as _json
    import logging as _logging

    from app import JsonFormatter

    record = _logging.LogRecord(
        name="app.collection",
        level=_logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="catching up after a metrics gap",
        args=(),
        exc_info=None,
    )
    record.source = "vcsim-lab"
    record.gap_hours = 383.0
    record.points = 6840

    payload = _json.loads(JsonFormatter().format(record))

    assert payload["message"] == "catching up after a metrics gap"
    assert payload["level"] == "WARNING"
    assert payload["source"] == "vcsim-lab"
    assert payload["gap_hours"] == 383.0
    assert payload["points"] == 6840
