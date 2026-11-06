"""Deterministic synthetic metrics with known, injected anomalies (spec 7.6).

Determinism matters twice over: the same VM produces the same series on every
run, so charts are stable across restarts; and the anomaly windows are known
exactly, so anomaly detection can be *scored* rather than eyeballed.

The RNG is seeded from a stable hash of the entity id - Python's built-in
`hash()` is salted per process and would break reproducibility.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from app.metrics.base import MetricEntity, MetricsProvider
from app.schemas import MetricPoint

log = logging.getLogger(__name__)

PROFILES = ("steady", "diurnal", "growing", "idle", "overprovisioned", "hot", "anomalous")

# Anomaly windows are positioned against this fixed epoch, never against "now".
#
# The anchor used to be the end of the generated range, which quietly broke the
# whole evaluation story: the backfill placed a spike 3 days before *its* end,
# then the collector's 60-second poll placed another one 3 days before *its*
# own end - a different absolute time, recorded in no ground truth file. Those
# phantom anomalies were 100 of the 103 false positives the detector was being
# blamed for. With a fixed epoch, every process computes the same absolute
# windows, so backfilled history and live collection agree and ground_truth.json
# is exact.
ANOMALY_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
DEFAULT_CYCLE_DAYS = 45

# Length of the trend ramp for the `growing` profile, in days.
#
# The ramp position is derived from absolute time - days since ANOMALY_EPOCH,
# modulo this period - for the same reason the anomaly windows are: it used to
# be the point's position inside whatever range was being generated, so a
# 180-day backfill produced a full ramp while a 60-second poll produced
# progress=0 and dropped the trend to nothing. Live points then disagreed with
# the history they were appended to.
#
# The modulo means the ramp restarts once per period. That is a real
# discontinuity in the synthetic data, and it is placed where nothing looks:
# anomaly detection reads 30 days, right-sizing 14, and the forecast holdout
# 30, so every evaluated window sits inside one ramp.
TREND_PERIOD_DAYS = 180

# Uniform draws consumed per generated point. This has to be an exact,
# fixed count, because the generator is fast-forwarded to a point's absolute
# grid position with `advance()`. Only `random()` is used: it consumes exactly
# one 64-bit word, whereas `normal()` uses rejection sampling and consumes a
# variable number, which silently desynchronises the stream. The two Gaussians
# are built from uniforms with Box-Muller instead.
DRAWS_PER_POINT = 7

# Mirrors the right-sizing rules' floors (ml/rightsizing.py): below these a
# downsize recommendation is never issued.
MIN_DOWNSIZE_VCPUS = 2
MIN_DOWNSIZE_MEMORY_MB = 4096


def stable_seed(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big")


@dataclass
class AnomalyWindow:
    entity_external_id: str
    kind: str
    start: datetime
    end: datetime
    magnitude: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_external_id": self.entity_external_id,
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "magnitude": round(self.magnitude, 2),
        }

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


@dataclass
class Scenarios:
    profiles: dict[str, dict[str, Any]]
    distribution: dict[str, float]
    anomaly_specs: list[dict[str, Any]]
    assignments: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Scenarios:
        data = yaml.safe_load(Path(path).read_text())
        return cls(
            profiles=data["profiles"],
            distribution=data.get("distribution", {}),
            anomaly_specs=data.get("anomalies", []),
            assignments=data.get("assignments", {}),
        )

    def profile_for(self, entity: MetricEntity) -> str:
        """Explicit assignment wins; otherwise the id hash picks from the
        distribution, which keeps the mix stable as inventory grows."""
        for needle, profile in self.assignments.items():
            if needle.lower() in entity.name.lower():
                return self._respecting_size(profile, entity)

        rng = np.random.default_rng(stable_seed(entity.external_id))
        names = list(self.distribution)
        weights = np.array([self.distribution[n] for n in names], dtype=float)
        weights = weights / weights.sum()
        return self._respecting_size(str(rng.choice(names, p=weights)), entity)

    @staticmethod
    def _respecting_size(profile: str, entity: MetricEntity) -> str:
        """Fall back when a profile makes no sense for the entity's size.

        `overprovisioned` means "far more resource than it uses", which is not
        a thing a 1-vCPU machine can be: the right-sizing rules will not suggest
        downsizing below 2 vCPUs or 4 GB, correctly, so labelling a tiny VM
        over-provisioned just creates a profile the platform can never act on.
        vcsim hands out 1 vCPU / 32 MB VMs, so without this guard most of the
        over-provisioned population was unactionable by construction.
        """
        if profile == "overprovisioned" and not (
            entity.vcpus > MIN_DOWNSIZE_VCPUS and entity.memory_mb > MIN_DOWNSIZE_MEMORY_MB
        ):
            return "steady"
        return profile


class SyntheticMetricsProvider(MetricsProvider):
    """Generates CPU, memory, disk and network series per entity profile."""

    def __init__(self, scenarios: Scenarios, interval_seconds: int = 300) -> None:
        self.scenarios = scenarios
        self.interval = timedelta(seconds=interval_seconds)
        self._profile_cache: dict[str, str] = {}
        self._anomaly_cache: dict[str, list[AnomalyWindow]] = {}

    # --- profile / anomaly assignment ------------------------------------

    def profile_for(self, entity: MetricEntity) -> str:
        if entity.external_id not in self._profile_cache:
            self._profile_cache[entity.external_id] = self.scenarios.profile_for(entity)
        return self._profile_cache[entity.external_id]

    def anomaly_windows(
        self, entity: MetricEntity, since: datetime, until: datetime
    ) -> list[AnomalyWindow]:
        """Injected windows overlapping [since, until] for an `anomalous` entity.

        Positions come from ANOMALY_EPOCH, so they are the same absolute
        timestamps in every process - the backfill, the live poll and the
        evaluator all agree.
        """
        if self.profile_for(entity) != "anomalous":
            return []

        cache_key = f"{entity.external_id}:{since.isoformat()}:{until.isoformat()}"
        cached = self._anomaly_cache.get(cache_key)
        if cached is not None:
            return cached

        windows: list[AnomalyWindow] = []
        for index, spec in enumerate(self.scenarios.anomaly_specs):
            # One RNG per (entity, spec) so adding a spec cannot move the others.
            rng = np.random.default_rng(stable_seed(f"{entity.external_id}:{index}") ^ 0xA5A5)
            cycle = timedelta(days=float(spec.get("cycle_days", DEFAULT_CYCLE_DAYS)))
            duration = timedelta(hours=float(spec.get("duration_hours", 2)))
            offset = timedelta(hours=float(rng.uniform(0, cycle.total_seconds() / 3600)))

            # Walk the cycle from the occurrence that could still overlap `since`.
            elapsed = (since - duration) - (ANOMALY_EPOCH + offset)
            k = max(0, int(elapsed // cycle))
            while True:
                start = ANOMALY_EPOCH + offset + k * cycle
                if start > until:
                    break
                if start + duration > since:
                    windows.append(
                        AnomalyWindow(
                            entity_external_id=entity.external_id,
                            kind=str(spec["kind"]),
                            start=start,
                            end=start + duration,
                            magnitude=float(spec.get("magnitude", 40)),
                        )
                    )
                k += 1

        windows.sort(key=lambda w: w.start)
        self._anomaly_cache[cache_key] = windows
        return windows

    # --- generation -------------------------------------------------------

    def collect(
        self,
        entities: list[MetricEntity],
        since: datetime,
        until: datetime | None = None,
    ) -> list[MetricPoint]:
        until = until or datetime.now(UTC)
        points: list[MetricPoint] = []
        for entity in entities:
            points.extend(self.series(entity, since, until))
        return points

    def _grid_start(self, since: datetime) -> datetime:
        """Snap to the interval grid.

        Every process then emits points at the same timestamps, so a 60-second
        poll of a 5-minute series produces one point on the grid instead of a
        fresh off-grid sample - which is what kept live data statistically
        different from the backfill it was appended to.
        """
        step = int(self.interval.total_seconds())
        epoch_seconds = int(since.timestamp())
        aligned = epoch_seconds - (epoch_seconds % step)
        return datetime.fromtimestamp(aligned, tz=UTC)

    def series(self, entity: MetricEntity, since: datetime, until: datetime) -> list[MetricPoint]:
        profile_name = self.profile_for(entity)
        params = {
            **self.scenarios.profiles.get("steady", {}),
            **self.scenarios.profiles.get(profile_name, {}),
        }
        windows = self.anomaly_windows(entity, since, until)

        # A powered-off VM still reports: near-zero everything.
        powered_off = entity.power_state != "on"

        points: list[MetricPoint] = []
        ts = self._grid_start(since)
        if ts < since:
            ts += self.interval

        # Seed from the entity, then fast-forward the stream to this point's
        # absolute position on the grid. The noise at a given timestamp is then
        # the same whether it was produced by a 180-day backfill or by a single
        # live poll - without this, a point's value depended on how many points
        # happened to precede it in the same call.
        step_seconds = int(self.interval.total_seconds())
        grid_index = int(ts.timestamp()) // step_seconds
        bit_generator = np.random.PCG64(stable_seed(entity.external_id))
        bit_generator.advance(grid_index * DRAWS_PER_POINT)
        rng = np.random.Generator(bit_generator)

        while ts <= until:
            cpu, mem, iops, latency, net = self._sample(
                params, rng, ts, _trend_progress(ts), powered_off=powered_off
            )
            for window in windows:
                if window.contains(ts):
                    cpu, mem, iops, latency, net = _apply_anomaly(
                        window, cpu, mem, iops, latency, net
                    )

            points.append(
                MetricPoint(
                    entity_type=entity.entity_type,
                    entity_external_id=entity.external_id,
                    ts=ts,
                    cpu_pct=_clamp(cpu, 0, 100),
                    mem_pct=_clamp(mem, 0, 100),
                    disk_iops=max(0.0, iops),
                    disk_latency_ms=max(0.0, latency),
                    net_kbps=max(0.0, net),
                )
            )
            ts += self.interval

        return points

    @staticmethod
    def _sample(
        params: dict[str, Any],
        rng: np.random.Generator,
        ts: datetime,
        progress: float,
        *,
        powered_off: bool,
    ) -> tuple[float, float, float, float, float]:
        # Always consume exactly DRAWS_PER_POINT values, even when the result is
        # discarded, so the stream stays aligned with the absolute grid.
        u1, u2, u3, u4, u5, u6, u7 = (float(rng.random()) for _ in range(DRAWS_PER_POINT))
        cpu_noise_draw = _box_muller(u1, u2)
        mem_noise_draw = _box_muller(u3, u4)
        iops_draw = 0.8 + 0.4 * u5
        latency_draw = 0.8 + 0.5 * u6
        net_draw = 0.7 + 0.6 * u7

        if powered_off:
            return 0.0, 0.0, 0.0, 0.0, 0.0

        cpu_base = float(params.get("cpu_base", 30))
        cpu_amplitude = float(params.get("cpu_amplitude", 0))
        cpu_noise = float(params.get("cpu_noise", 3))
        trend_per_day = float(params.get("cpu_trend_per_day", 0))
        weekend_factor = float(params.get("weekend_factor", 1.0))
        total_days = float(params.get("_total_days", 180))

        hour = ts.hour + ts.minute / 60.0
        # Business-hours peak at 14:00; the sine is shifted so 02:00 is the trough.
        daily = math.sin((hour - 8.0) / 24.0 * 2 * math.pi) if cpu_amplitude else 0.0
        seasonal = cpu_amplitude * max(daily, -0.6)
        weekend = weekend_factor if ts.weekday() >= 5 else 1.0
        trend = trend_per_day * progress * total_days

        cpu = (cpu_base + seasonal + trend) * weekend + cpu_noise_draw * cpu_noise

        mem_base = float(params.get("mem_base", 45))
        mem_noise = float(params.get("mem_noise", 2))
        mem_trend = float(params.get("mem_trend_per_day", 0))
        mem = mem_base + mem_trend * progress * total_days + mem_noise_draw * mem_noise

        # Disk and network track CPU: a busy VM is busy everywhere.
        activity = max(0.05, cpu / 100.0)
        iops = float(params.get("iops_base", 120)) * activity * iops_draw
        latency = float(params.get("latency_base", 2.0)) * (1 + activity) * latency_draw
        net = float(params.get("net_base", 500)) * activity * net_draw

        return cpu, mem, iops, latency, net


def _box_muller(u1: float, u2: float) -> float:
    """One standard normal from two uniforms, at a fixed cost of two draws."""
    return math.sqrt(-2.0 * math.log(max(u1, 1e-12))) * math.cos(2.0 * math.pi * u2)


def _trend_progress(ts: datetime) -> float:
    """Position in the trend ramp, in [0, 1), from absolute time alone."""
    days = (ts - ANOMALY_EPOCH).total_seconds() / 86400.0
    return (days % TREND_PERIOD_DAYS) / TREND_PERIOD_DAYS


def _apply_anomaly(
    window: AnomalyWindow, cpu: float, mem: float, iops: float, latency: float, net: float
) -> tuple[float, float, float, float, float]:
    if window.kind == "cpu_spike":
        cpu = min(100.0, cpu + window.magnitude)
        iops *= 2.0
        net *= 2.5
    elif window.kind == "latency_storm":
        latency *= max(2.0, window.magnitude / 5.0)
        iops *= 0.4
        cpu = max(0.0, cpu - 5.0)
    elif window.kind == "memory_leak":
        mem = min(100.0, mem + window.magnitude)
        cpu += window.magnitude / 4.0
    elif window.kind == "network_flood":
        net *= max(3.0, window.magnitude / 8.0)
        cpu += window.magnitude / 3.0
    return cpu, mem, iops, latency, net


def _clamp(value: float, low: float, high: float) -> float:
    return float(min(max(value, low), high))


_provider: SyntheticMetricsProvider | None = None


def get_metrics_provider(
    scenarios_path: str | None = None, interval_seconds: int = 300
) -> SyntheticMetricsProvider:
    global _provider
    if _provider is None:
        from app.config import settings

        path = scenarios_path or settings.scenarios_path
        _provider = SyntheticMetricsProvider(
            Scenarios.load(path), interval_seconds=interval_seconds
        )
        log.info("synthetic metrics provider ready", extra={"scenarios": path})
    return _provider
