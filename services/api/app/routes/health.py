from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import text

from app.db.mongo import get_db
from app.db.postgres import SessionLocal

router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, object]:
    """Liveness + dependency probe used by the compose healthcheck."""
    checks: dict[str, str] = {}
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - health must never raise
        checks["postgres"] = f"error: {exc.__class__.__name__}"
    try:
        await get_db().command("ping")
        checks["mongo"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["mongo"] = f"error: {exc.__class__.__name__}"

    healthy = all(v == "ok" for v in checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks, "ts": datetime.now(UTC)}
