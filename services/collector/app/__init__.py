"""Flask application factory for the collector."""

from __future__ import annotations

import logging
import sys

from flask import Flask, jsonify

from app.config import settings


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}')
    )
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
