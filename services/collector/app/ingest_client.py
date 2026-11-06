"""HTTP client for the API's /internal endpoints.

The collector owns no database connection; this module is the only way data
leaves the service (spec 2, "Service boundaries").
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.schemas import InventoryBatch, MetricPoint, MetricsBatch, SourceConfig

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


class IngestClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.api_internal_url).rstrip("/")
        self.token = token or settings.internal_service_token

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"X-Service-Token": self.token},
            timeout=timeout or settings.http_timeout_seconds,
        )

    def _post(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        try:
            with self._client(timeout) as client:
                response = client.post(path, json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise IngestError(f"POST {path} -> {exc.response.status_code}: {body}") from exc
        except httpx.HTTPError as exc:
            raise IngestError(f"POST {path} failed: {exc}") from exc

    def fetch_sources(self) -> list[SourceConfig]:
        """Re-read on every run so a source added in the UI is picked up without
        restarting the collector (spec 7.7)."""
        try:
            with self._client(timeout=15.0) as client:
                response = client.get("/sources")
                response.raise_for_status()
                return [SourceConfig.model_validate(row) for row in response.json()]
        except httpx.HTTPError as exc:
            raise IngestError(f"GET /sources failed: {exc}") from exc

    def post_inventory(self, batch: InventoryBatch) -> dict[str, Any]:
        return self._post("/inventory", batch.model_dump(mode="json"))

    def post_metrics(self, source_id: UUID, points: list[MetricPoint]) -> dict[str, Any]:
        batch = MetricsBatch(source_id=source_id, points=points)
        return self._post("/metrics", batch.model_dump(mode="json"), timeout=300.0)

    def post_metrics_chunked(
        self, source_id: UUID, points: list[MetricPoint], chunk_size: int | None = None
    ) -> int:
        """180 days x 5-minute resolution is millions of points; ship them in
        chunks so a single request never carries the whole backfill."""
        size = chunk_size or settings.metrics_chunk_size
        sent = 0
        for start in range(0, len(points), size):
            chunk = points[start : start + size]
            self.post_metrics(source_id, chunk)
            sent += len(chunk)
        return sent

    def report_status(
        self,
        source_id: UUID,
        status: str,
        error: str | None = None,
        collected_at: datetime | None = None,
    ) -> None:
        payload: dict[str, Any] = {"status": status, "error": error}
        if collected_at is not None:
            payload["collected_at"] = collected_at.isoformat()
        try:
            self._post(f"/sources/{source_id}/status", payload, timeout=15.0)
        except IngestError:
            # Status reporting is best-effort: losing it must not fail the run.
            log.warning("could not report status for source %s", source_id)
