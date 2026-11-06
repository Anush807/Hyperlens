"""In-API background jobs (spec 8.3).

Hourly: roll metrics up into metrics_daily, then detect anomalies over the last
24 hours. Daily at 02:00: regenerate forecasts and recommendations.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.mongo import get_db
from app.db.postgres import SessionLocal
from app.services import metrics_service
from app.services.ml_state import state

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def rollup_job() -> None:
    try:
        await metrics_service.rollup_daily(get_db(), days=2)
    except Exception:
        log.exception("rollup job failed")


async def anomaly_job() -> None:
    from ml.jobs import run_anomaly_detection

    try:
        async with SessionLocal() as session:
            result = await run_anomaly_detection(session, get_db(), days=1)
        state.record_run({result.name: result.to_dict()})
    except Exception:
        log.exception("anomaly job failed")


async def nightly_job() -> None:
    from ml.jobs import run_forecasts, run_rightsizing

    try:
        async with SessionLocal() as session:
            forecast_result = await run_forecasts(session, get_db(), days=180)
            rightsizing_result = await run_rightsizing(session, get_db())
        state.record_run(
            {
                forecast_result.name: forecast_result.to_dict(),
                rightsizing_result.name: rightsizing_result.to_dict(),
            }
        )
    except Exception:
        log.exception("nightly job failed")


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    # Offset the anomaly job so it reads a rollup that has already finished.
    scheduler.add_job(rollup_job, CronTrigger(minute=5), id="rollup", max_instances=1)
    scheduler.add_job(anomaly_job, CronTrigger(minute=15), id="anomaly", max_instances=1)
    scheduler.add_job(nightly_job, CronTrigger(hour=2, minute=0), id="nightly", max_instances=1)
    scheduler.start()
    _scheduler = scheduler
    log.info("scheduler started", extra={"jobs": [j.id for j in scheduler.get_jobs()]})
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
