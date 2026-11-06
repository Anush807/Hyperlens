"""Collector configuration. The collector holds no database credentials: it
learns about sources from the API over /internal (spec 7.7)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    api_internal_url: str = "http://api:8000/internal"
    internal_service_token: str = "change-me-too"

    poll_inventory_seconds: int = 300
    poll_metrics_seconds: int = 60
    enable_scheduler: bool = True

    # Connection defaults; per-source values from the API win over these.
    vsphere_insecure: bool = True
    vsphere_port: int = 8989
    http_timeout_seconds: float = 60.0

    # Synthetic metrics
    scenarios_path: str = "/app/synthetic/scenarios.yaml"
    metrics_interval_seconds: int = 300
    metrics_chunk_size: int = 5000

    # Catching up after an outage.
    #
    # The metrics job used to generate only `poll_metrics_seconds` of history,
    # so anything the collector was down for was lost for good. It now asks the
    # API where each source left off and generates forward from there.
    #
    # The catch-up is deliberately chunked rather than done in one batch: a
    # multi-day gap across ~100 entities is millions of points, and one poll
    # should not try to produce them all. Each cycle advances by at most
    # `metrics_catchup_chunk_hours`, so the hole closes over several cycles.
    metrics_catchup_enabled: bool = True
    metrics_catchup_chunk_hours: int = 12
    # Past this, catching up automatically is the wrong call - say so and point
    # the operator at `python -m synthetic.generate_history` instead.
    metrics_max_catchup_days: int = 30

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
