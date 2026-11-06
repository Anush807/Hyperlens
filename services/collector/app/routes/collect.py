"""Manual trigger used by the API's "Sync now" button."""

from __future__ import annotations

import logging
from uuid import UUID

from flask import Blueprint, jsonify, request

from app.collection import run_inventory_cycle, run_metrics_cycle
from app.config import settings

log = logging.getLogger(__name__)
bp = Blueprint("collect", __name__, url_prefix="/collect")


def _authorized() -> bool:
    token = request.headers.get("X-Service-Token", "")
    return bool(token) and token == settings.internal_service_token


@bp.post("/trigger")
def trigger():
    if not _authorized():
        return jsonify({"error": {"code": "unauthorized", "message": "Invalid service token"}}), 401

    payload = request.get_json(silent=True) or {}
    raw_source_id = payload.get("source_id")
    source_id: UUID | None = None
    if raw_source_id:
        try:
            source_id = UUID(str(raw_source_id))
        except ValueError:
            return (
                jsonify({"error": {"code": "bad_request", "message": "source_id is not a UUID"}}),
                400,
            )

    results = run_inventory_cycle(source_id=source_id)
    failed = [r for r in results if not r.get("ok")]
    return jsonify(
        {
            "detail": f"collected {len(results)} source(s), {len(failed)} failed",
            "results": results,
        }
    )


@bp.post("/metrics")
def trigger_metrics():
    if not _authorized():
        return jsonify({"error": {"code": "unauthorized", "message": "Invalid service token"}}), 401
    results = run_metrics_cycle()
    return jsonify(
        {"detail": f"generated metrics for {len(results)} source(s)", "results": results}
    )
