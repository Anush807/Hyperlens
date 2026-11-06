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

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
