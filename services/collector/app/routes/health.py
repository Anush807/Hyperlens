from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, jsonify

from app.collection import cached_entity_count
from app.config import settings
from app.scheduler import last_runs

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """Liveness plus the last run time per job (spec 7.7)."""
    return jsonify(
        {
            "status": "ok",
            "ts": datetime.now(UTC).isoformat(),
            "scheduler_enabled": settings.enable_scheduler,
            "cached_entities": cached_entity_count(),
            "jobs": {
                name: {k: v for k, v in run.items() if k != "results"}
                for name, run in last_runs.items()
            },
        }
    )
