"""The collector and the API each own a copy of the ingest schema (spec 6.1).
This test is what keeps the two copies honest."""

from __future__ import annotations

import json
from pathlib import Path

import app.schemas as collector_schemas

API_SCHEMA = Path("/app/../api/app/schemas/ingest.py")

MODELS = [
    "ClusterIn",
    "HostIn",
    "VMIn",
    "DatastoreIn",
    "InventoryBatch",
    "MetricPoint",
    "MetricsBatch",
]


def test_every_shared_model_exists_on_the_collector_side():
    for name in MODELS:
        assert hasattr(collector_schemas, name), f"{name} missing from collector schemas"


def test_metric_point_fields_match_the_documented_contract():
    fields = set(collector_schemas.MetricPoint.model_fields)
    assert fields == {
        "entity_type",
        "entity_external_id",
        "ts",
        "cpu_pct",
        "mem_pct",
        "disk_iops",
        "disk_latency_ms",
        "net_kbps",
    }


def test_inventory_batch_serialises_to_the_ingest_payload():
    batch = collector_schemas.InventoryBatch(
        source_id="11111111-1111-1111-1111-111111111111",
        collected_at="2026-06-01T00:00:00Z",
        vms=[
            collector_schemas.VMIn(
                external_id="v1",
                name="vm",
                vcpus=2,
                memory_mb=4096,
                provisioned_storage_gb=40,
                power_state="on",
            )
        ],
    )
    payload = json.loads(batch.model_dump_json())
    assert payload["source_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["vms"][0]["power_state"] == "on"
    assert payload["clusters"] == []


def test_power_state_maps_are_total_for_known_vendor_values():
    assert set(collector_schemas.VSPHERE_POWER_STATES.values()) <= {"on", "off", "suspended"}
    assert (
        collector_schemas.normalize_power_state("nonsense", collector_schemas.VSPHERE_POWER_STATES)
        == "unknown"
    )
