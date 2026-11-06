"""Flask application factory for the collector."""

from __future__ import annotations

import json
import logging
import sys

from flask import Flask, jsonify

from app.config import settings

# Attributes logging puts on every LogRecord. Anything else came from an
# `extra=` dict and is ours to emit.
_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Emit the record *and* its `extra=` fields as JSON.

    The previous formatter was a hardcoded format string that interpolated only
    level, logger and message. Every structured field this service attaches -
    source names, point counts, gap sizes, adapter errors - was silently thrown
    away, which made a failing collector almost unreadable from its own logs.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def create_app(start_jobs: bool | None = None) -> Flask:
    _configure_logging()
    app = Flask(__name__)

    from app.routes import collect, health

    app.register_blueprint(health.bp)
    app.register_blueprint(collect.bp)

    @app.errorhandler(404)
    def _not_found(_):
        return jsonify({"error": {"code": "not_found", "message": "No such route"}}), 404

    @app.errorhandler(Exception)
    def _unhandled(exc):
        logging.getLogger(__name__).exception("unhandled error")
        return (
            jsonify({"error": {"code": "internal_error", "message": exc.__class__.__name__}}),
            500,
        )

    should_start = settings.enable_scheduler if start_jobs is None else start_jobs
    if should_start:
        from app.scheduler import start_scheduler

        start_scheduler()

    return app
