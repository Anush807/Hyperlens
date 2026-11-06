"""Metrics provider strategy (spec 7.1).

V1 ships only the synthetic provider. A real provider (vSphere performance
counters, Prism stats) implements the same two methods and drops in through
`get_metrics_provider` with no change above this line.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from app.schemas import MetricPoint

EntityKind = Literal["vm", "host"]


class MetricEntity:
    """The minimum an entity needs for metrics to be generated or fetched."""

    __slots__ = ("entity_type", "external_id", "name", "vcpus", "memory_mb", "power_state")

    def __init__(
        self,
        entity_type: EntityKind,
        external_id: str,
        name: str,
        vcpus: int = 0,
        memory_mb: int = 0,
        power_state: str = "on",
    ) -> None:
        self.entity_type = entity_type
        self.external_id = external_id
        self.name = name
        self.vcpus = vcpus
        self.memory_mb = memory_mb
        self.power_state = power_state

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MetricEntity({self.entity_type}:{self.name})"


class MetricsProvider(ABC):
    @abstractmethod
    def collect(
        self, entities: list[MetricEntity], since: datetime, until: datetime | None = None
    ) -> list[MetricPoint]:
        """Points for every entity in [since, until]."""
