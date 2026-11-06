from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks

from app.config import settings
from app.db.mongo import get_db
from app.db.postgres import SessionLocal
from app.deps import AdminUser, DbSession
from app.schemas.ml import MlStatus
from app.services import audit
from app.services.ml_state import state

router = APIRouter(prefix="/ml", tags=["ml"])
log = logging.getLogger(__name__)


async def _run_jobs(anomaly_days: int, history_days: int) -> None:
    """Runs on its own session: the request's session is long gone by then."""
    from ml.jobs import run_all

    state.running = True
    try:
        async with SessionLocal() as session:
            results = await run_all(
                session, get_db(), anomaly_days=anomaly_days, history_days=history_days
            )
        state.record_run(results)
        log.info("ml run complete", extra={"jobs": list(results)})
    except Exception:
        log.exception("ml run failed")
    finally:
        state.running = False


@router.post("/run", response_model=dict)
async def run_ml(
    session: DbSession,
    admin: AdminUser,
    background: BackgroundTasks,
    anomaly_days: int = 7,
    history_days: int = 180,
) -> dict[str, Any]:
    if state.running:
        return {"queued": False, "detail": "an ML run is already in progress"}
    background.add_task(_run_jobs, anomaly_days, history_days)
    await audit.record(
        session,
        user_id=admin.id,
        action="ml.run",
        details={"anomaly_days": anomaly_days, "history_days": history_days},
    )
    await session.commit()
    return {"queued": True, "detail": "anomaly detection, forecasting and right-sizing started"}


@router.post("/reload", response_model=dict)
async def reload_models(session: DbSession, admin: AdminUser) -> dict[str, Any]:
    result = state.reload()
    await audit.record(session, user_id=admin.id, action="ml.reload", details=result)
    await session.commit()
    return result


@router.get("/status", response_model=MlStatus)
async def ml_status(_: AdminUser) -> MlStatus:
    snapshot = state.snapshot()
    return MlStatus(
        anomaly=snapshot["anomaly"],
        forecast=snapshot["forecast"],
        rightsizing=snapshot["rightsizing"],
        artifact_dir=settings.ml_artifact_dir,
        loaded_at=snapshot["loaded_at"],
    )
