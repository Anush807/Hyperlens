"""One collection run per source, with per-source failure isolation (spec 7.7).

A source that fails is reported as failed and skipped; every other source
continues. That is the behaviour Phase 3's acceptance test checks by stopping
the Nutanix mock and expecting vSphere to keep working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.adapters.base import AdapterError
from app.adapters.factory import get_adapter
from app.config import settings
from app.ingest_client import IngestClient, IngestError
from app.metrics.base import MetricEntity
from app.metrics.synthetic import get_metrics_provider
from app.schemas import InventoryBatch, SourceConfig

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    source_id: UUID
    source_name: str
    ok: bool
    error: str | None = None
    # Floats as well as ints: catch-up reports gap sizes in hours.
    counts: dict[str, float] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "source": self.source_name,
            "ok": self.ok,
            "error": self.error,
            "counts": self.counts,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# Inventory from the last successful run, reused by the metrics job so it does
# not have to re-walk the hypervisor every minute.
_entity_cache: dict[UUID, list[MetricEntity]] = {}


def collect_inventory(source: SourceConfig, client: IngestClient) -> RunResult:
    result = RunResult(source_id=source.id, source_name=source.name, ok=False)
    collected_at = datetime.now(UTC)
    try:
        adapter = get_adapter(source)
        with adapter:
            clusters = adapter.list_clusters()
            hosts = adapter.list_hosts()
            vms = adapter.list_vms()
            datastores = adapter.list_datastores()

        batch = InventoryBatch(
            source_id=source.id,
            collected_at=collected_at,
            clusters=clusters,
            hosts=hosts,
            vms=vms,
            datastores=datastores,
        )
        response = client.post_inventory(batch)

        _entity_cache[source.id] = _to_entities(vms, hosts)
        result.ok = True
        result.counts = {
            "clusters": len(clusters),
            "hosts": len(hosts),
            "vms": len(vms),
            "datastores": len(datastores),
            **{k: v for k, v in response.items() if isinstance(v, int)},
        }
        log.info("inventory collected", extra={"source": source.name, **result.counts})
    except (AdapterError, IngestError) as exc:
        result.error = str(exc)[:1000]
        log.warning(
            "inventory collection failed", extra={"source": source.name, "error": result.error}
        )
    except Exception as exc:  # noqa: BLE001 - never let one source kill the job
        result.error = f"{exc.__class__.__name__}: {exc}"[:1000]
        log.exception("unexpected inventory failure", extra={"source": source.name})
    finally:
        result.finished_at = datetime.now(UTC)
        client.report_status(
            source.id,
            "ok" if result.ok else "failed",
            result.error,
            collected_at if result.ok else None,
        )
    return result


def _to_entities(vms: list[Any], hosts: list[Any]) -> list[MetricEntity]:
    entities = [
        MetricEntity("vm", vm.external_id, vm.name, vm.vcpus, vm.memory_mb, vm.power_state)
        for vm in vms
    ]
    entities += [
        MetricEntity(
            "host", host.external_id, host.name, host.cpu_cores, host.memory_mb, host.power_state
        )
        for host in hosts
    ]
    return entities


@dataclass
class MetricWindow:
    """The span a metrics cycle will generate, and why."""

    since: datetime
    until: datetime
    catching_up: bool = False
    gap_hours: float = 0.0
    remaining_hours: float = 0.0
    truncated: bool = False


def resolve_metric_window(
    watermark: datetime | None,
    now: datetime,
    *,
    window_seconds: int | None = None,
    chunk_hours: int | None = None,
    max_catchup_days: int | None = None,
) -> MetricWindow:
    """Decide what span to generate, given where this source left off.

    Three cases:

    * An explicit `window_seconds` wins. That is what a manual trigger and the
      tests use, and it keeps the old behaviour available deliberately.
    * No watermark at all - a brand new source. Generate one poll interval.
      Backfilling a new source's history is `generate_history`'s job, not
      something a 60-second poll should start doing unprompted.
    * A watermark exists. Generate from it forward, which is the fix: whatever
      the collector was down for is still ahead of the watermark and gets
      produced instead of silently skipped.

    A long gap is capped twice. `chunk_hours` bounds one cycle so a multi-day
    hole does not become one enormous batch - successive cycles walk forward
    until it closes. `max_catchup_days` is the point where automatic recovery
    stops being sensible at all; beyond it the window is clamped and the caller
    is told to run the backfill tool.
    """
    poll = timedelta(seconds=window_seconds or settings.poll_metrics_seconds)
    if window_seconds is not None or not settings.metrics_catchup_enabled:
        return MetricWindow(since=now - poll, until=now)

    if watermark is None:
        return MetricWindow(since=now - poll, until=now)

    # Start just after the watermark, not at it. The watermark *is* a generated
    # point and it is already stored, so beginning there would re-emit it: the
    # grid snapping floors to the same slot and ships a duplicate every cycle.
    since = watermark + timedelta(seconds=1)
    gap = now - watermark

    # Only a gap larger than a couple of generation intervals is a recovery
    # worth announcing. Points land on a 5-minute grid while the poll runs every
    # 60 seconds, so in healthy operation the watermark is routinely minutes
    # old - measuring against the poll interval alone labelled every normal
    # cycle a catch-up and logged a warning for it.
    quiet = timedelta(
        seconds=max(settings.poll_metrics_seconds, settings.metrics_interval_seconds * 2)
    )
    chunk = timedelta(hours=chunk_hours or settings.metrics_catchup_chunk_hours)
    cap = timedelta(days=max_catchup_days or settings.metrics_max_catchup_days)

    truncated = False
    if gap > cap:
        since = now - cap
        gap = cap
        truncated = True

    until = min(now, since + chunk)
    return MetricWindow(
        since=since,
        until=until,
        catching_up=gap > quiet,
        gap_hours=round(gap.total_seconds() / 3600, 2),
        remaining_hours=round((now - until).total_seconds() / 3600, 2),
        truncated=truncated,
    )


def collect_metrics(
    source: SourceConfig,
    client: IngestClient,
    *,
    window_seconds: int | None = None,
    watermark: datetime | None = None,
) -> RunResult:
    """Generate and ship the points for the outstanding window.

    Normally that is the interval that just elapsed. After an outage it is
    everything since this source's last ingested point, in chunks.
    """
    result = RunResult(source_id=source.id, source_name=source.name, ok=False)
    try:
        entities = _entity_cache.get(source.id)
        if not entities:
            result.ok = True
            result.counts = {"points": 0, "skipped": 1}
            return result

        provider = get_metrics_provider(interval_seconds=settings.metrics_interval_seconds)
        window = resolve_metric_window(watermark, datetime.now(UTC), window_seconds=window_seconds)
        points = provider.collect(entities, window.since, window.until)
        sent = client.post_metrics_chunked(source.id, points)

        result.ok = True
        result.counts = {"points": sent, "entities": len(entities)}
        if window.catching_up:
            result.counts["catchup_gap_hours"] = window.gap_hours
            result.counts["catchup_remaining_hours"] = window.remaining_hours
            log.warning(
                "catching up after a metrics gap",
                extra={
                    "source": source.name,
                    "gap_hours": window.gap_hours,
                    "generated_from": window.since.isoformat(),
                    "generated_to": window.until.isoformat(),
                    "remaining_hours": window.remaining_hours,
                    "points": sent,
                },
            )
            if window.truncated:
                log.error(
                    "metrics gap exceeds metrics_max_catchup_days; the remainder will not be "
                    "generated automatically - run `python -m synthetic.generate_history "
                    "--from <date> --to <date>` to fill it",
                    extra={"source": source.name, "cap_days": settings.metrics_max_catchup_days},
                )
        else:
            log.info("metrics collected", extra={"source": source.name, **result.counts})
    except IngestError as exc:
        result.error = str(exc)[:1000]
        log.warning(
            "metrics collection failed", extra={"source": source.name, "error": result.error}
        )
    except Exception as exc:  # noqa: BLE001
        result.error = f"{exc.__class__.__name__}: {exc}"[:1000]
        log.exception("unexpected metrics failure", extra={"source": source.name})
    finally:
        result.finished_at = datetime.now(UTC)
    return result


def run_inventory_cycle(
    client: IngestClient | None = None, source_id: UUID | None = None
) -> list[dict[str, Any]]:
    client = client or IngestClient()
    try:
        sources = client.fetch_sources()
    except IngestError as exc:
        log.error("could not fetch sources from the API: %s", exc)
        return [{"ok": False, "error": str(exc)}]

    if source_id is not None:
        sources = [s for s in sources if s.id == source_id]
        if not sources:
            return [{"ok": False, "error": f"source {source_id} not found or disabled"}]

    return [collect_inventory(source, client).to_dict() for source in sources]


def run_metrics_cycle(client: IngestClient | None = None) -> list[dict[str, Any]]:
    client = client or IngestClient()
    try:
        sources = client.fetch_sources()
    except IngestError as exc:
        log.error("could not fetch sources from the API: %s", exc)
        return [{"ok": False, "error": str(exc)}]

    # One watermark call per cycle, not per source. Degrades to {} on failure,
    # which falls back to generating a single interval.
    watermarks = client.fetch_watermarks() if settings.metrics_catchup_enabled else {}
    return [
        collect_metrics(source, client, watermark=watermarks.get(source.id)).to_dict()
        for source in sources
    ]


def cached_entity_count() -> int:
    return sum(len(v) for v in _entity_cache.values())
