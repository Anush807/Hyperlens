"""Deterministic ML tests on small synthetic series (spec 13).

No database and no Mongo: these exercise the models themselves, so a failure
points at the maths rather than at the plumbing.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml import anomaly, forecast, rightsizing
from ml.data import FEATURE_COLUMNS, add_derived_features, drop_partial_windows

HOUR = pd.Timedelta(hours=1)


def hourly_frame(
    entities: int = 4, days: int = 30, *, spikes: dict[str, list[int]] | None = None
) -> pd.DataFrame:
    """A well-behaved diurnal series per entity, optionally with spikes injected
    at given hour offsets."""
    start = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    rng = np.random.default_rng(7)
    for e in range(entities):
        entity_id = f"vm-{e}"
        level = 20 + 10 * e
        for hour in range(days * 24):
            ts = start + hour * HOUR
            diurnal = 8 * math.sin((ts.hour - 8) / 24 * 2 * math.pi)
            weekend = 0.6 if ts.dayofweek >= 5 else 1.0
            cpu = (level + diurnal) * weekend + rng.normal(0, 1.0)
            mem = 45 + rng.normal(0, 1.0)
            lat = 2.0 + rng.normal(0, 0.1)
            if spikes and hour in spikes.get(entity_id, []):
                cpu += 55
                lat *= 8
            rows.append(
                {
                    "entity_external_id": entity_id,
                    "window_start": ts,
                    "cpu_mean": cpu,
                    "cpu_std": 1.0,
                    "cpu_p95": cpu + 2,
                    "mem_mean": mem,
                    "mem_std": 1.0,
                    "mem_p95": mem + 2,
                    "lat_mean": lat,
                    "lat_std": 0.1,
                    "lat_p95": lat + 0.2,
                    "iops_mean": 100 + cpu,
                    "net_mean": 400 + cpu * 5,
                    "samples": 12,
                }
            )
    return add_derived_features(pd.DataFrame(rows))


# --- features -------------------------------------------------------------


def test_derived_features_are_present_and_finite():
    df = hourly_frame()
    for column in FEATURE_COLUMNS:
        assert column in df.columns
    assert np.isfinite(df[FEATURE_COLUMNS].to_numpy()).all()


def test_a_steady_series_has_near_zero_deviations():
    df = hourly_frame()
    settled = df[df["window_start"] >= df["window_start"].min() + pd.Timedelta(days=10)]
    assert settled[FEATURE_COLUMNS].abs().max(axis=1).median() < 5


def test_weekend_hours_are_not_treated_as_deviations():
    """The baseline compares like with like; without that, every weekend hour
    looks anomalous on a VM that idles at the weekend."""
    df = hourly_frame()
    settled = df[df["window_start"] >= df["window_start"].min() + pd.Timedelta(days=10)]
    weekend = settled[settled["window_start"].dt.dayofweek >= 5]
    weekday = settled[settled["window_start"].dt.dayofweek < 5]
    assert weekend["dev_cpu_mean"].abs().median() < 5
    assert weekday["dev_cpu_mean"].abs().median() < 5


def test_partial_windows_are_dropped():
    df = hourly_frame(entities=1, days=2)
    df.loc[df.index[-1], "samples"] = 2
    kept = drop_partial_windows(df)
    assert len(kept) == len(df) - 1


# --- anomaly detection ----------------------------------------------------


def test_training_requires_data():
    with pytest.raises(ValueError, match="no hourly windows"):
        anomaly.train(pd.DataFrame())


def test_injected_spikes_are_flagged():
    spike_hours = [400, 401, 402]
    df = hourly_frame(spikes={"vm-1": spike_hours})
    model = anomaly.train(df, flag_rate=0.01)

    scored = anomaly.score_all(model, df)
    flagged = scored[scored["is_anomaly"]]

    spiked = flagged[flagged["entity_external_id"] == "vm-1"]
    offsets = {int((ts - df["window_start"].min()) / HOUR) for ts in spiked["window_start"]}
    assert set(spike_hours) & offsets, "the injected spike should be flagged"


def test_quiet_entities_are_not_flagged():
    df = hourly_frame(spikes={"vm-1": [400, 401]})
    model = anomaly.train(df, flag_rate=0.01)
    scored = anomaly.score_all(model, df)
    flagged = scored[scored["is_anomaly"]]
    assert (flagged["entity_external_id"] == "vm-1").all()


def test_the_deviation_gate_is_enforced():
    df = hourly_frame()
    model = anomaly.train(df, flag_rate=0.5, min_deviation=1000.0)
    scored = anomaly.score_all(model, df)
    # The forest would flag half the windows; nothing clears a 1000-sigma gate.
    assert not scored["is_anomaly"].any()


def test_consecutive_flagged_hours_merge_into_one_incident():
    base = pd.Timestamp("2026-02-01", tz="UTC")
    flagged = pd.DataFrame(
        [
            {"entity_external_id": "vm-1", "window_start": base + i * HOUR, "score": -0.2 - i / 100}
            for i in range(5)
        ]
        + [{"entity_external_id": "vm-1", "window_start": base + 20 * HOUR, "score": -0.3}]
        + [{"entity_external_id": "vm-2", "window_start": base, "score": -0.25}]
    )
    for column in FEATURE_COLUMNS:
        flagged[column] = 12.0

    incidents = anomaly.merge_windows(flagged)

    by_entity = {}
    for incident in incidents:
        by_entity.setdefault(incident["entity_external_id"], []).append(incident)
    assert len(by_entity["vm-1"]) == 2
    assert by_entity["vm-1"][0]["windows"] == 5
    assert by_entity["vm-1"][0]["window_end"] - by_entity["vm-1"][0]["window_start"] == 5 * HOUR
    assert len(by_entity["vm-2"]) == 1


def test_window_scoring_matches_hand_counted_labels():
    base = pd.Timestamp("2026-03-01", tz="UTC")
    scored = pd.DataFrame(
        {
            "entity_external_id": ["vm-1"] * 4,
            "window_start": [base + i * HOUR for i in range(4)],
            "is_anomaly": [True, True, False, True],
        }
    )
    truth = [
        {
            "entity_external_id": "vm-1",
            "start": base.isoformat(),
            "end": (base + 3 * HOUR).isoformat(),
        }
    ]

    result = anomaly.score_windows(scored, truth)

    # Hours 0,1,2 are anomalous; we flagged 0,1,3.
    assert result["true_positive_windows"] == 2
    assert result["false_positive_windows"] == 1
    assert result["false_negative_windows"] == 1
    assert result["precision"] == pytest.approx(2 / 3, abs=1e-3)
    assert result["recall"] == pytest.approx(2 / 3, abs=1e-3)


def test_ground_truth_outside_the_scored_period_is_excluded():
    base = pd.Timestamp("2026-03-01", tz="UTC")
    incidents = [
        {
            "entity_external_id": "vm-1",
            "window_start": base,
            "window_end": base + HOUR,
            "score": -0.3,
        }
    ]
    truth = [
        {"entity_external_id": "vm-1", "start": base.isoformat(), "end": (base + HOUR).isoformat()},
        # A year earlier: never scored, so it cannot count against recall.
        {
            "entity_external_id": "vm-1",
            "start": "2025-03-01T00:00:00+00:00",
            "end": "2025-03-01T05:00:00+00:00",
        },
    ]

    scored = anomaly.score_against_ground_truth(
        incidents, truth, scored_from=base, scored_to=base + HOUR
    )
    assert scored["injected"] == 1
    assert scored["incident_recall"] == 1.0


# --- forecasting ----------------------------------------------------------


def wide_frame(days: int = 150, slope: float = 0.0, level: float = 40.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=days, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    data = {}
    for name, extra_slope in (("a", slope), ("b", slope * 0.5), ("c", 0.0)):
        values = [
            level + extra_slope * i + 4 * math.sin(2 * math.pi * (i % 7) / 7) + rng.normal(0, 0.4)
            for i in range(days)
        ]
        data[name] = values
    return pd.DataFrame(data, index=index)


def test_seasonal_naive_repeats_last_week():
    history = np.arange(14, dtype=float)
    predicted = forecast.seasonal_naive(history, horizon=10, season=7)
    assert list(predicted[:7]) == list(history[-7:])
    assert predicted[7] == history[-7]


def test_seasonal_naive_handles_short_history():
    assert list(forecast.seasonal_naive(np.array([5.0]), horizon=3)) == [5.0, 5.0, 5.0]
    assert list(forecast.seasonal_naive(np.array([]), horizon=2)) == [0.0, 0.0]


def test_training_rejects_too_little_history():
    with pytest.raises(ValueError, match="at least"):
        forecast.train_lstm(wide_frame(days=40), max_epochs=1)


def test_windows_target_the_change_not_the_level():
    """Targets are deltas from the last observed value, which is what lets a
    growing series be predicted above anything in its training range."""
    frame = wide_frame(days=120, slope=0.3)
    scalers = {c: forecast.EntityScaler.fit(frame[c].to_numpy()) for c in frame.columns}
    windows = forecast.build_windows(frame, scalers)

    assert windows.x.shape[1:] == (forecast.LOOKBACK, 3)
    assert windows.y.shape[1] == forecast.HORIZON
    # A rising series has positive deltas on average.
    assert windows.y.mean() > 0


def test_a_growing_series_is_forecast_upward_and_hits_the_threshold():
    frame = wide_frame(days=150, slope=0.45, level=45.0)
    artifacts = forecast.train_lstm(frame, max_epochs=12, patience=3)

    history = frame["a"].to_numpy()
    dates = pd.DatetimeIndex(frame.index)
    predicted = forecast.predict(artifacts, "a", history, dates)

    assert predicted[-1] > predicted[0], "a rising series should keep rising"
    points = forecast.to_points(predicted, dates[-1].date(), artifacts.residual_quantiles)
    assert forecast.days_to_threshold(points, 85.0) is not None


def test_a_flat_series_never_reaches_the_threshold():
    points = forecast.to_points(np.full(30, 30.0), datetime.now(UTC).date())
    assert forecast.days_to_threshold(points, 85.0) is None


def test_points_are_clamped_and_bands_bracket_the_value():
    values = np.array([95.0, 150.0, -20.0])
    points = forecast.to_points(
        values, datetime(2026, 5, 1).date(), {"p10": [-5, -5, -5], "p90": [5, 5, 5]}
    )

    assert [p["value"] for p in points] == [95.0, 100.0, 0.0]
    assert all(p["lower"] <= p["value"] <= p["upper"] for p in points)
    assert points[0]["date"] == "2026-05-02"


def test_mae_and_smape_are_zero_for_a_perfect_forecast():
    actual = np.array([10.0, 20.0, 30.0])
    assert forecast.mae(actual, actual) == 0.0
    assert forecast.smape(actual, actual) == pytest.approx(0.0, abs=1e-6)


def test_smape_tolerates_zeroes():
    assert forecast.smape(np.zeros(3), np.zeros(3)) == pytest.approx(0.0, abs=1e-6)


def test_holdout_evaluation_reports_both_models_per_profile():
    frame = wide_frame(days=150, slope=0.3)
    artifacts = forecast.train_lstm(frame, max_epochs=8, patience=3)

    result = forecast.evaluate_holdout(
        artifacts, frame, profiles={"a": "growing", "b": "growing", "c": "steady"}
    )

    assert set(result["overall"]) >= {"lstm_mae", "naive_mae", "lstm_beats_naive"}
    assert set(result["by_profile"]) == {"growing", "steady"}
    for row in result["by_profile"].values():
        assert "lstm_beats_naive" in row


def test_artifacts_round_trip_through_disk(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    frame = wide_frame(days=150, slope=0.2)
    artifacts = forecast.train_lstm(frame, max_epochs=4, patience=2)
    forecast.save(artifacts, version=3)

    loaded = forecast.load()

    assert loaded is not None
    assert loaded.version == 3
    assert set(loaded.scalers) == set(artifacts.scalers)
    dates = pd.DatetimeIndex(frame.index)
    before = forecast.predict(artifacts, "a", frame["a"].to_numpy(), dates)
    after = forecast.predict(loaded, "a", frame["a"].to_numpy(), dates)
    assert np.allclose(before, after, atol=1e-5)


def test_loading_missing_artifacts_returns_none(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ml_artifact_dir", str(tmp_path))
    assert forecast.load() is None
    assert anomaly.load() is None


# --- right-sizing ---------------------------------------------------------


def daily_window(
    days: int = 14, *, cpu_p95: float = 50.0, mem_p95: float = 60.0, net: float = 500.0
) -> pd.DataFrame:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    return pd.DataFrame(
        [
            {
                "entity_external_id": "v1",
                "date": pd.Timestamp(start + timedelta(days=i)),
                "cpu_pct_p95": cpu_p95,
                "mem_pct_p95": mem_p95,
                "net_kbps_mean": net,
            }
            for i in range(days)
        ]
    )


def spec(vcpus: int = 8, memory_mb: int = 16384, power: str = "on") -> rightsizing.VMSpec:
    return rightsizing.VMSpec("v1", "vm-1", vcpus, memory_mb, power)


def kinds(suggestions) -> set[str]:
    return {s.kind for s in suggestions}


def test_idle_vm_is_flagged_idle_and_nothing_else():
    suggestions = rightsizing.evaluate_vm(spec(), daily_window(cpu_p95=2.0, mem_p95=15.0, net=4.0))
    assert kinds(suggestions) == {"idle"}
    assert suggestions[0].evidence["cpu_pct_p95"] == 2.0
    assert "rule" in suggestions[0].evidence


def test_a_busy_but_quiet_vm_is_not_idle():
    suggestions = rightsizing.evaluate_vm(spec(), daily_window(cpu_p95=40.0, net=2.0))
    assert "idle" not in kinds(suggestions)


def test_overprovisioned_vm_is_downsized_to_the_computed_target():
    suggestions = rightsizing.evaluate_vm(spec(vcpus=8), daily_window(cpu_p95=15.0, mem_p95=20.0))
    cpu = next(s for s in suggestions if s.kind == "downsize_cpu")
    # ceil(8 * 15 / 50) = 3
    assert cpu.current_value == 8
    assert cpu.suggested_value == 3


def test_downsize_never_goes_below_two_vcpus():
    suggestions = rightsizing.evaluate_vm(
        spec(vcpus=4), daily_window(cpu_p95=1.0, mem_p95=50.0, net=100.0)
    )
    cpu = next(s for s in suggestions if s.kind == "downsize_cpu")
    assert cpu.suggested_value == 2


def test_a_two_vcpu_vm_is_never_cpu_downsized():
    suggestions = rightsizing.evaluate_vm(
        spec(vcpus=2), daily_window(cpu_p95=5.0, mem_p95=50.0, net=100.0)
    )
    assert "downsize_cpu" not in kinds(suggestions)


def test_memory_downsize_rounds_up_to_a_whole_gb_and_respects_the_floor():
    suggestions = rightsizing.evaluate_vm(
        spec(vcpus=8, memory_mb=32768), daily_window(cpu_p95=40.0, mem_p95=20.0)
    )
    mem = next(s for s in suggestions if s.kind == "downsize_mem")
    # round_up_to_gb(32768 * 20 / 60) = 11264
    assert mem.suggested_value == 11264
    assert mem.suggested_value % 1024 == 0

    small = rightsizing.evaluate_vm(
        spec(vcpus=8, memory_mb=4096), daily_window(cpu_p95=40.0, mem_p95=5.0)
    )
    assert "downsize_mem" not in kinds(small)


def test_a_hot_vm_is_upsized_by_half():
    window = daily_window(cpu_p95=92.0, mem_p95=95.0)
    suggestions = rightsizing.evaluate_vm(spec(vcpus=4, memory_mb=8192), window)

    cpu = next(s for s in suggestions if s.kind == "upsize_cpu")
    mem = next(s for s in suggestions if s.kind == "upsize_mem")
    assert cpu.suggested_value == 6  # ceil(4 * 1.5)
    assert mem.suggested_value == 12288
    assert cpu.evidence["hot_days"] == 14


def test_upsize_needs_five_hot_days_out_of_fourteen():
    window = daily_window(cpu_p95=40.0)
    window.loc[window.index[:4], "cpu_pct_p95"] = 95.0
    assert "upsize_cpu" not in kinds(rightsizing.evaluate_vm(spec(), window))

    window.loc[window.index[:5], "cpu_pct_p95"] = 95.0
    assert "upsize_cpu" in kinds(rightsizing.evaluate_vm(spec(), window))


def test_powered_off_vms_are_skipped():
    assert rightsizing.evaluate_vm(spec(power="off"), daily_window(cpu_p95=1.0)) == []


def test_too_little_history_produces_nothing():
    assert rightsizing.evaluate_vm(spec(), daily_window(days=3, cpu_p95=1.0)) == []


def test_evaluate_all_only_looks_at_the_last_fourteen_days():
    window = daily_window(days=40, cpu_p95=2.0, mem_p95=10.0, net=1.0)
    # Make the oldest 26 days busy: they must not change the verdict.
    window.loc[window.index[:26], "cpu_pct_p95"] = 95.0
    suggestions = rightsizing.evaluate_all([spec()], window)
    assert kinds(suggestions) == {"idle"}
