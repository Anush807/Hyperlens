"""Backfill N days of synthetic metric history (spec 7.6).

    python -m synthetic.generate_history --days 180

Walks each configured source for its VMs and hosts, generates a deterministic
series per entity, and POSTs it to the API's ingest endpoint in chunks. The
injected anomaly windows are written to ground_truth.json so anomaly detection
can be scored rather than guessed at.

Points are generated and shipped one entity at a time: 180 days at 5-minute
resolution is ~52k points per entity, and holding the whole estate in memory
would be gigabytes for no benefit.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.adapters.base import AdapterError
from app.adapters.factory import get_adapter
from app.collection import _to_entities
from app.config import settings
from app.ingest_client import IngestClient, IngestError
from app.metrics.base import MetricEntity
from app.metrics.synthetic import SyntheticMetricsProvider, Scenarios

log = logging.getLogger("synthetic.generate_history")

GROUND_TRUTH_PATHS = [
    Path("/ml_artifacts/ground_truth.json"),  # shared volume: the API reads it here
    Path(__file__).parent / "ground_truth.json",
]


def _entities_for_source(source: Any) -> list[MetricEntity]:
    adapter = get_adapter(source)
    with adapter:
        vms = adapter.list_vms()
        hosts = adapter.list_hosts()
    return _to_entities(vms, hosts)


def generate(
    days: int,
    interval_seconds: int,
    chunk_size: int,
    *,
    only_source: str | None = None,
    dry_run: bool = False,
    start: datetime | None = None,
    end: datetime | None = None,
    ground_truth_only: bool = False,
) -> dict[str, Any]:
    if ground_truth_only and only_source is not None:
        raise SystemExit(
            "--ground-truth-only describes every source's injected windows, so it "
            "cannot be combined with --source"
        )
    # It computes the windows and writes the file; it never ingests.
    dry_run = dry_run or ground_truth_only

    client = IngestClient()
    provider = SyntheticMetricsProvider(
        Scenarios.load(settings.scenarios_path), interval_seconds=interval_seconds
    )

    # An explicit range beats `--days`, which can only ever count back from now.
    # Filling a known hole in the middle of the history needs real endpoints -
    # without them the only way to cover a gap was to regenerate everything,
    # re-posting months of points that were already there.
    if start is not None or end is not None:
        until = (end or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
        since = (start or (until - timedelta(days=days))).replace(
            minute=0, second=0, microsecond=0
        )
        if since >= until:
            raise SystemExit(f"--from ({since.isoformat()}) must be before --to ({until.isoformat()})")
    else:
        until = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        since = until - timedelta(days=days)

    sources = client.fetch_sources()
    if only_source:
        sources = [s for s in sources if s.name == only_source or str(s.id) == only_source]
    if not sources:
        raise SystemExit("no enabled sources found - add one in /settings/sources first")

    totals = {"entities": 0, "points": 0, "sources": len(sources), "failed_sources": 0}
    ground_truth: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "history_start": since.isoformat(),
        "history_end": until.isoformat(),
        "interval_seconds": interval_seconds,
        "profiles": {},
        "anomaly_windows": [],
    }

    started = time.perf_counter()
    for source in sources:
        try:
            entities = _entities_for_source(source)
        except AdapterError as exc:
            log.error("skipping source %s: %s", source.name, exc)
            totals["failed_sources"] += 1
            continue

        log.info("backfilling source", extra={"source": source.name, "entities": len(entities)})
        for index, entity in enumerate(entities, start=1):
            profile = provider.profile_for(entity)
            ground_truth["profiles"][entity.external_id] = profile
            for window in provider.anomaly_windows(entity, since, until):
                ground_truth["anomaly_windows"].append(window.to_dict())

            points = provider.series(entity, since, until)
            totals["entities"] += 1
            totals["points"] += len(points)

            if not dry_run:
                try:
                    client.post_metrics_chunked(source.id, points, chunk_size=chunk_size)
                except IngestError as exc:
                    log.error("ingest failed for %s: %s", entity.name, exc)
                    raise

            if index % 10 == 0 or index == len(entities):
                elapsed = time.perf_counter() - started
                log.info(
                    "progress",
                    extra={
                        "source": source.name,
                        "entity": f"{index}/{len(entities)}",
                        "points": totals["points"],
                        "elapsed_s": round(elapsed, 1),
                    },
                )

    totals["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    totals["anomaly_windows"] = len(ground_truth["anomaly_windows"])

    # Only a full regeneration may write ground truth.
    #
    # This file is the entire injected anomaly set, and `python -m ml.evaluate`
    # scores against it. A dry run or a targeted gap fill covers a slice - one
    # source, a day and a half - and writing it from there silently replaced 180
    # days of windows across 95 entities with that slice's handful. The
    # evaluation then reported against truth that described almost nothing.
    #
    # A partial refill never needs to: the windows are positioned from a fixed
    # epoch, so the existing file already describes the range being refilled.
    partial = [
        reason
        for reason, applies in (
            ("dry run", dry_run),
            ("single source", only_source is not None),
            ("explicit range", start is not None or end is not None),
        )
        if applies
    ]
    if ground_truth_only:
        # The whole point of the flag: recompute the truth file and nothing else.
        _write_ground_truth(ground_truth)
        totals["ground_truth"] = "written (ground-truth-only)"
    elif partial:
        totals["ground_truth"] = f"not written ({', '.join(partial)})"
        log.info(
            "leaving ground_truth.json alone", extra={"reason": totals["ground_truth"]}
        )
    else:
        _write_ground_truth(ground_truth)
    return totals


def _write_ground_truth(payload: dict[str, Any]) -> None:
    """Written to every reachable location: the shared artifact volume is what
    `python -m ml.evaluate` reads inside the API container."""
    written = []
    for path in GROUND_TRUTH_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2))
            written.append(str(path))
        except OSError as exc:
            log.debug("could not write ground truth to %s: %s", path, exc)
    if not written:
        log.warning("ground truth could not be written anywhere; evaluation will skip scoring")
    else:
        log.info("wrote ground truth", extra={"paths": written})


def _parse_when(value: str | None, flag: str) -> datetime | None:
    """Accept a date or a datetime, and assume UTC when no offset is given."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(
            f"{flag}: {value!r} is not an ISO date or datetime (e.g. 2026-10-22 or "
            "2026-10-22T06:00)"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format='{"level":"%(levelname)s","message":"%(message)s"}'
    )
    parser = argparse.ArgumentParser(prog="synthetic.generate_history")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="seconds between points (300 = 5 min, the spec default; raise it on a laptop)",
    )
    parser.add_argument("--chunk", type=int, default=5000)
    parser.add_argument("--source", default=None, help="limit to one source name or id")
    parser.add_argument(
        "--from",
        dest="start",
        default=None,
        metavar="WHEN",
        help="start of the range, e.g. 2026-10-22 or 2026-10-22T06:00. Overrides --days",
    )
    parser.add_argument(
        "--to",
        dest="end",
        default=None,
        metavar="WHEN",
        help="end of the range (default: now). Use with --from to fill a known gap",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="generate and validate but ingest nothing, and leave ground_truth.json alone",
    )
    parser.add_argument(
        "--ground-truth-only",
        action="store_true",
        help=(
            "rewrite ground_truth.json from the scenarios and current inventory without "
            "ingesting anything. Use after a targeted refill, or to rebuild the file "
            "that `python -m ml.evaluate` scores against"
        ),
    )
    args = parser.parse_args(argv)

    result = generate(
        args.days,
        args.interval,
        args.chunk,
        only_source=args.source,
        dry_run=args.dry_run,
        start=_parse_when(args.start, "--from"),
        end=_parse_when(args.end, "--to"),
        ground_truth_only=args.ground_truth_only,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
