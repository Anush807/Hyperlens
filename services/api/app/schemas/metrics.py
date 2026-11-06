from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Resolution = Literal["raw", "hourly", "daily"]

METRIC_FIELDS = ("cpu_pct", "mem_pct", "disk_iops", "disk_latency_ms", "net_kbps")


class MetricRow(BaseModel):
    ts: datetime
    cpu_pct: float | None = None
    mem_pct: float | None = None
    disk_iops: float | None = None
    disk_latency_ms: float | None = None
    net_kbps: float | None = None
    # Present for hourly/daily rows only.
    cpu_pct_p95: float | None = None
    mem_pct_p95: float | None = None
    cpu_pct_max: float | None = None
    mem_pct_max: float | None = None


class MetricSeries(BaseModel):
    entity_type: Literal["vm", "host"]
    entity_id: str
    entity_name: str
    resolution: Resolution
    from_ts: datetime
    to_ts: datetime
    points: list[MetricRow]


class MetricStats(BaseModel):
    metric: str
    days: int
    mean: float
    p95: float
    max: float
    trend_slope_per_day: float
    samples: int
