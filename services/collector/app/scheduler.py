"""APScheduler jobs (spec 7.7). One background scheduler per process."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from app.collection import run_inventory_cycle, run_metrics_cycle
from app.config import settings

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
last_runs: dict[str, dict[str, Any]] = {}


def _record(job: str, results: list[dict[str, Any]]) -> None:
    last_runs[job] = {
        "ran_at": datetime.now(UTC).isoformat(),
        "sources": len(results),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }


def inventory_job() -> None:
    try:
        _record("inventory", run_inventory_cycle())
    except Exception:
        log.exception("inventory job crashed")


def metrics_job() -> None:
    try:
        _record("metrics", run_metrics_cycle())
    except Exception:
        log.exception("metrics job crashed")


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.enable_scheduler or _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        inventory_job,
        "interval",
        seconds=settings.poll_inventory_seconds,
        id="inventory",
        max_instances=1,
        coalesce=True,
        # Give the API time to finish migrating and seeding before the first poll.
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        metrics_job,
        "interval",
        seconds=settings.poll_metrics_seconds,
        id="metrics",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "collector scheduler started",
        extra={
            "inventory_seconds": settings.poll_inventory_seconds,
            "metrics_seconds": settings.poll_metrics_seconds,
        },
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
