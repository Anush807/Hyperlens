"""Per-source failure isolation and the metric collection cycle (spec 7.7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app import collection
from app.adapters.base import AdapterError, HypervisorAdapter
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
