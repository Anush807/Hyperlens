"""Single source of truth for runtime configuration.

Everything comes from the environment so the same image runs in compose, in
tests (via monkeypatched env) and on a developer laptop.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Postgres
    database_url: str = "postgresql+asyncpg://hvops:hvops@postgres:5432/hvops"

    # Mongo
    mongo_url: str = "mongodb://mongo:27017"
    mongo_db: str = "hvops"
    metrics_ttl_days: int = 180
    events_ttl_days: int = 30

    # Auth
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_min: int = 15
    jwt_refresh_ttl_days: int = 7
    seed_admin_email: str = "admin@hvops.local"
    seed_admin_password: str = "admin123"
    fernet_key: str = "JDMvXWJ2m8V0cM0LhV8lFBWQxYc2Lh7Yq0K9Yy1n4Vo="

    # Internal service-to-service auth
    internal_service_token: str = "change-me-too"
    collector_url: str = "http://collector:5000"

    # LLM
    llm_base_url: str = "http://llm:8080/v1"
    llm_model: str = "qwen2.5-7b-instruct"
    llm_api_key: str = "not-needed"
    llm_ctx: int = 8192
    llm_temperature: float = 0.2
    llm_timeout_seconds: float = 120.0
    llm_max_tool_rounds: int = 4

    # ML
    ml_artifact_dir: str = "/ml_artifacts"
    forecast_horizon_days: int = 30
    forecast_lookback_days: int = 28
    cpu_threshold_pct: float = 85.0
    mem_threshold_pct: float = 85.0
    datastore_threshold_pct: float = 90.0

    # HTTP
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    enable_scheduler: bool = True
    log_level: str = "INFO"

    # Set by the test suite. Each test runs in its own event loop, and a pooled
    # asyncpg connection cannot be reused across loops, so tests connect without
    # a pool. Nothing else changes.
    testing: bool = False

    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
