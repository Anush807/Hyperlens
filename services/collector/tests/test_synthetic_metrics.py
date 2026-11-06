"""The synthetic provider is the ground truth for every ML acceptance test, so
its determinism and its profile behaviour are themselves tested."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.metrics.base import MetricEntity
from app.metrics.synthetic import Scenarios, SyntheticMetricsProvider, stable_seed

SCENARIOS_PATH = Path(__file__).resolve().parents[1] / "synthetic" / "scenarios.yaml"
if not SCENARIOS_PATH.exists():  # in the image synthetic/ sits beside app/
    SCENARIOS_PATH = Path("/app/synthetic/scenarios.yaml")

# Chosen so a 90-day lookback sits inside a single trend ramp: the ramp is a
# 180-day sawtooth (see TREND_PERIOD_DAYS), and a window straddling the reset
# legitimately shows no net growth.
ANCHOR = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def scenarios() -> Scenarios:
    return Scenarios.load(SCENARIOS_PATH)


@pytest.fixture
def provider(scenarios) -> SyntheticMetricsProvider:
    return SyntheticMetricsProvider(scenarios, interval_seconds=1800)


def entity(name: str, external_id: str | None = None, power: str = "on") -> MetricEntity:
    return MetricEntity("vm", external_id or name, name, 4, 8192, power)


def daily_p95(points, attr: str) -> list[float]:
    by_day: dict[str, list[float]] = {}
    for point in points:
        by_day.setdefault(point.ts.date().isoformat(), []).append(getattr(point, attr))
    out = []
    for values in by_day.values():
        values.sort()
        out.append(values[max(0, int(round(0.95 * (len(values) - 1))))])
    return out


def test_stable_seed_is_process_independent():
    """Python's built-in hash() is salted per process; this must not be."""
    assert stable_seed("DC0_H0_VM0") == stable_seed("DC0_H0_VM0")
    assert stable_seed("a") != stable_seed("b")
    assert stable_seed("DC0_H0_VM0") == 6376821109960828280


def test_same_entity_produces_an_identical_series(provider):
    since = ANCHOR - timedelta(days=2)
    first = provider.series(entity("DC0_H0_VM5"), since, ANCHOR)
    second = provider.series(entity("DC0_H0_VM5"), since, ANCHOR)

    assert len(first) == len(second)
    assert [p.cpu_pct for p in first] == [p.cpu_pct for p in second]


def test_explicit_assignments_win_over_the_hash_distribution(provider):
    assert provider.profile_for(entity("DC0_H0_VM0")) == "growing"
    assert provider.profile_for(entity("DC0_H0_VM1")) == "idle"
    assert provider.profile_for(entity("nx-vm-004")) == "hot"


def test_idle_profile_stays_under_the_rightsizing_threshold(provider):
    points = provider.series(entity("DC0_H0_VM1"), ANCHOR - timedelta(days=14), ANCHOR)
    assert max(daily_p95(points, "cpu_pct")) < 5.0
    assert statistics.fmean(p.net_kbps for p in points) < 10.0


def test_hot_profile_stays_above_the_upsize_threshold(provider):
    points = provider.series(entity("nx-vm-004"), ANCHOR - timedelta(days=14), ANCHOR)
    hot_days = sum(1 for value in daily_p95(points, "cpu_pct") if value > 85.0)
    assert hot_days >= 5


def test_overprovisioned_profile_lands_between_idle_and_busy(provider):
    points = provider.series(entity("DC0_C1_RP0_VM0"), ANCHOR - timedelta(days=14), ANCHOR)
    p95s = daily_p95(points, "cpu_pct")
    assert 5.0 < statistics.fmean(p95s) < 20.0


def test_growing_profile_trends_upward(provider):
    points = provider.series(entity("DC0_H0_VM0"), ANCHOR - timedelta(days=90), ANCHOR)
    p95s = daily_p95(points, "cpu_pct")
    first_fortnight = statistics.fmean(p95s[:14])
    last_fortnight = statistics.fmean(p95s[-14:])
    assert last_fortnight > first_fortnight + 5.0


def test_diurnal_profile_peaks_during_business_hours(provider):
    points = provider.series(entity("DC0_H0_VM7"), ANCHOR - timedelta(days=7), ANCHOR)
    business = [p.cpu_pct for p in points if 9 <= p.ts.hour <= 17 and p.ts.weekday() < 5]
    overnight = [p.cpu_pct for p in points if p.ts.hour in (1, 2, 3)]
    if provider.profile_for(entity("DC0_H0_VM7")) in {"diurnal", "growing", "anomalous"}:
        assert statistics.fmean(business) > statistics.fmean(overnight)


def test_powered_off_entities_report_zero(provider):
    points = provider.series(entity("DC0_H0_VM9", power="off"), ANCHOR - timedelta(days=1), ANCHOR)
    assert all(p.cpu_pct == 0 and p.mem_pct == 0 and p.net_kbps == 0 for p in points)


def test_anomalous_entities_get_reproducible_injected_windows(provider, scenarios):
    target = entity("DC0_C0_RP0_VM0")
    windows = provider.anomaly_windows(target, ANCHOR - timedelta(days=180), ANCHOR)

    # Each spec recurs on its own cycle, so a 180-day range holds several.
    assert len(windows) >= len(scenarios.anomaly_specs)
    assert {w.kind for w in windows} == {str(spec["kind"]) for spec in scenarios.anomaly_specs}
    assert all(w.end > w.start for w in windows)
    assert all(w.start < ANCHOR for w in windows)
    # Same anchor -> identical windows, which is what ground_truth.json records.
    again = SyntheticMetricsProvider(scenarios, interval_seconds=1800).anomaly_windows(
        target, ANCHOR - timedelta(days=180), ANCHOR
    )
    assert [w.to_dict() for w in windows] == [w.to_dict() for w in again]


def test_non_anomalous_entities_get_no_windows(provider):
    assert (
        provider.anomaly_windows(entity("DC0_H0_VM1"), ANCHOR - timedelta(days=180), ANCHOR) == []
    )


def test_injected_window_actually_moves_the_metrics(provider):
    target = entity("DC0_C0_RP0_VM0")
    spike = next(
        w
        for w in provider.anomaly_windows(target, ANCHOR - timedelta(days=180), ANCHOR)
        if w.kind == "cpu_spike"
    )

    inside = provider.series(target, spike.start, spike.end - timedelta(minutes=1))
    quiet_start = spike.start - timedelta(days=1)
    outside = provider.series(target, quiet_start, quiet_start + (spike.end - spike.start))

    assert statistics.fmean(p.cpu_pct for p in inside) > statistics.fmean(
        p.cpu_pct for p in outside
    )


def test_collect_covers_every_entity(provider):
    entities = [entity(f"vm-{i}") for i in range(5)]
    points = provider.collect(entities, ANCHOR - timedelta(hours=6), ANCHOR)
    assert {p.entity_external_id for p in points} == {e.external_id for e in entities}


def test_values_stay_in_range(provider):
    points = provider.series(entity("nx-vm-004"), ANCHOR - timedelta(days=3), ANCHOR)
    assert all(0 <= p.cpu_pct <= 100 for p in points)
    assert all(0 <= p.mem_pct <= 100 for p in points)
    assert all(p.disk_iops >= 0 and p.disk_latency_ms >= 0 and p.net_kbps >= 0 for p in points)


def test_anomaly_windows_are_absolute_not_relative_to_the_request(provider):
    """A 60-second live poll and a 180-day backfill must agree on where the
    injected anomalies are - the bug this guards against put phantom anomalies
    into live data that no ground truth file recorded."""
    target = entity("DC0_C0_RP0_VM0")
    wide = provider.anomaly_windows(target, ANCHOR - timedelta(days=180), ANCHOR)
    assert wide, "the anomalous profile should have windows in a 180-day range"

    sample = wide[-1]
    narrow = provider.anomaly_windows(
        target, sample.start + timedelta(minutes=30), sample.start + timedelta(minutes=31)
    )
    assert [w.start for w in narrow] == [sample.start]


def test_points_land_on_the_interval_grid(provider):
    """Repeated polls must produce the same timestamps, so a replayed batch is
    a duplicate rather than a new, slightly different sample."""
    start = ANCHOR - timedelta(hours=3) + timedelta(seconds=137)
    points = provider.series(entity("vm-grid"), start, ANCHOR)
    step = int(provider.interval.total_seconds())
    assert all(int(p.ts.timestamp()) % step == 0 for p in points)


def test_the_same_timestamp_yields_the_same_value_in_any_request(provider):
    long_run = {
        p.ts: p.cpu_pct
        for p in provider.series(entity("DC0_H0_VM0"), ANCHOR - timedelta(days=3), ANCHOR)
    }
    short_run = provider.series(entity("DC0_H0_VM0"), ANCHOR - timedelta(hours=2), ANCHOR)
    overlapping = [p for p in short_run if p.ts in long_run]
    assert overlapping
    assert all(long_run[p.ts] == p.cpu_pct for p in overlapping)
