"""Read-only tools the assistant can call (spec 10.2).

They call the service layer directly rather than looping back through HTTP:
fewer moving parts, and the calling user's role travels with the context so a
viewer cannot reach anything a viewer could not reach in the UI.

Outputs are compact on purpose - the context window is 8k tokens, so lists are
truncated and floats are rounded to one decimal place.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Cluster, Datastore, Host, User
from app.db.models.enums import (
    AnomalyStatus,
    EntityType,
    PowerState,
    RecommendationKind,
    RecommendationStatus,
)
from app.schemas.metrics import METRIC_FIELDS
from app.services import inventory_service, metrics_service, ml_service

log = logging.getLogger(__name__)

MAX_LIST = 20
MAX_DAYS = 90


@dataclass
class ToolContext:
    session: AsyncSession
    mongo: AsyncIOMotorDatabase
    user: User


def r1(value: float | None) -> float | None:
    return round(float(value), 1) if value is not None else None


# --- Argument schemas -----------------------------------------------------


class GetInventorySummaryArgs(BaseModel):
    cluster_name: str | None = None


class SearchVMsArgs(BaseModel):
    name_contains: str | None = None
    cluster_name: str | None = None
    power_state: Literal["on", "off", "suspended", "unknown"] | None = None
    min_vcpus: int | None = Field(default=None, ge=1, le=256)
    limit: int = Field(default=10, ge=1, le=MAX_LIST)


class GetVMDetailsArgs(BaseModel):
    vm_name: str = Field(min_length=1)


class GetMetricStatsArgs(BaseModel):
    entity_type: Literal["vm", "host"] = "vm"
    name: str = Field(min_length=1)
    metric: Literal["cpu_pct", "mem_pct", "disk_iops", "disk_latency_ms", "net_kbps"] = "cpu_pct"
    days: int = Field(default=14, ge=1, le=MAX_DAYS)


class ListAnomaliesArgs(BaseModel):
    status: Literal["open", "acknowledged", "resolved"] | None = "open"
    days: int = Field(default=7, ge=1, le=MAX_DAYS)
    limit: int = Field(default=10, ge=1, le=MAX_LIST)


class ListRecommendationsArgs(BaseModel):
    kind: Literal["downsize_cpu", "downsize_mem", "upsize_cpu", "upsize_mem", "idle"] | None = None
    cluster_name: str | None = None
    limit: int = Field(default=10, ge=1, le=MAX_LIST)


class GetCapacityForecastArgs(BaseModel):
    cluster_name: str | None = None
    datastore_name: str | None = None


# --- Lookups --------------------------------------------------------------


async def _find_cluster(session: AsyncSession, name: str) -> Cluster | None:
    row = (
        await session.execute(
            select(Cluster).where(func.lower(Cluster.name) == name.lower()).limit(1)
        )
    ).scalar_one_or_none()
    if row:
        return row
    return (
        await session.execute(select(Cluster).where(Cluster.name.ilike(f"%{name}%")).limit(1))
    ).scalar_one_or_none()


async def _find_datastore(session: AsyncSession, name: str) -> Datastore | None:
    return (
        await session.execute(select(Datastore).where(Datastore.name.ilike(f"%{name}%")).limit(1))
    ).scalar_one_or_none()


async def _find_host(session: AsyncSession, name: str) -> Host | None:
    return (
        await session.execute(select(Host).where(Host.name.ilike(f"%{name}%")).limit(1))
    ).scalar_one_or_none()


# --- Implementations ------------------------------------------------------


async def get_inventory_summary(ctx: ToolContext, args: GetInventorySummaryArgs) -> dict[str, Any]:
    summary = await inventory_service.summary(ctx.session)
    clusters = summary.per_cluster
    if args.cluster_name:
        needle = args.cluster_name.lower()
        clusters = [c for c in clusters if needle in c.cluster_name.lower()]
        if not clusters:
            return {"error": f"no cluster matching {args.cluster_name!r}"}

    return {
        "totals": {
            "clusters": summary.clusters,
            "hosts": summary.hosts,
            "vms": summary.vms,
            "vms_powered_on": summary.vms_powered_on,
            "datastores": summary.datastores,
            "allocated_vcpus": summary.total_vcpus,
            "physical_cores": summary.total_physical_cores,
            "allocated_memory_gb": r1(summary.total_vm_memory_mb / 1024),
            "physical_memory_gb": r1(summary.total_physical_memory_mb / 1024),
            "open_anomalies": summary.open_anomalies,
            "open_recommendations": summary.open_recommendations,
            "reclaimable_vcpus": summary.reclaimable_vcpus,
            "reclaimable_memory_gb": r1(summary.reclaimable_memory_mb / 1024),
        },
        "clusters": [
            {
                "name": c.cluster_name,
                "source": str(c.source_type),
                "hosts": c.host_count,
                "vms": c.vm_count,
                "physical_cores": c.physical_cores,
                "allocated_vcpus": c.allocated_vcpus,
                "vcpu_overcommit": r1(c.vcpu_overcommit_ratio),
                "memory_overcommit": r1(c.memory_overcommit_ratio),
            }
            for c in clusters[:MAX_LIST]
        ],
    }


async def search_vms(ctx: ToolContext, args: SearchVMsArgs) -> dict[str, Any]:
    cluster_id: uuid.UUID | None = None
    if args.cluster_name:
        cluster = await _find_cluster(ctx.session, args.cluster_name)
        if cluster is None:
            return {"error": f"no cluster matching {args.cluster_name!r}", "vms": []}
        cluster_id = cluster.id

    items, total = await inventory_service.list_vms(
        ctx.session,
        source_id=None,
        cluster_id=cluster_id,
        host_id=None,
        power_state=PowerState(args.power_state) if args.power_state else None,
        q=args.name_contains,
        include_deleted=False,
        min_vcpus=args.min_vcpus,
        limit=args.limit,
        offset=0,
    )
    return {
        "total_matching": total,
        "returned": len(items),
        "vms": [
            {
                "name": vm.name,
                "cluster": vm.cluster_name,
                "host": vm.host_name,
                "vcpus": vm.vcpus,
                "memory_gb": r1(vm.memory_mb / 1024),
                "power_state": str(vm.power_state),
                "source": str(vm.source_type) if vm.source_type else None,
            }
            for vm in items
        ],
    }


async def get_vm_details(ctx: ToolContext, args: GetVMDetailsArgs) -> dict[str, Any]:
    vm = await inventory_service.find_vm_by_name(ctx.session, args.vm_name)
    if vm is None:
        return {"error": f"no VM matching {args.vm_name!r}"}

    latest = await inventory_service.latest_metrics(ctx.mongo, "vm", vm.external_id)
    anomalies, _ = await ml_service.list_anomalies(
        ctx.session, entity_id=vm.id, status=AnomalyStatus.OPEN, limit=5, offset=0
    )
    recs, _ = await ml_service.list_recommendations(
        ctx.session, vm_id=vm.id, status=RecommendationStatus.OPEN, limit=5, offset=0
    )
    cpu = await metrics_service.metric_stats(
        ctx.mongo, entity_type="vm", external_id=vm.external_id, metric="cpu_pct", days=14
    )
    mem = await metrics_service.metric_stats(
        ctx.mongo, entity_type="vm", external_id=vm.external_id, metric="mem_pct", days=14
    )

    return {
        "name": vm.name,
        "cluster": vm.cluster_name,
        "host": vm.host_name,
        "source": str(vm.source_type) if vm.source_type else None,
        "vcpus": vm.vcpus,
        "memory_gb": r1(vm.memory_mb / 1024),
        "storage_gb": r1(vm.provisioned_storage_gb),
        "guest_os": vm.guest_os,
        "power_state": str(vm.power_state),
        "latest_metrics": {
            "ts": latest.ts.isoformat() if latest.ts else None,
            "cpu_pct": r1(latest.cpu_pct),
            "mem_pct": r1(latest.mem_pct),
            "disk_latency_ms": r1(latest.disk_latency_ms),
        },
        "last_14_days": {
            "cpu_pct_mean": cpu.mean,
            "cpu_pct_p95": cpu.p95,
            "mem_pct_mean": mem.mean,
            "mem_pct_p95": mem.p95,
        },
        "open_anomalies": [
            {
                "detected_at": a.detected_at.date().isoformat(),
                "score": r1(a.score),
                "summary": a.summary,
            }
            for a in anomalies
        ],
        "open_recommendations": [
            {
                "kind": str(r.kind),
                "current": r1(r.current_value),
                "suggested": r1(r.suggested_value),
            }
            for r in recs
        ],
    }


async def get_metric_stats(ctx: ToolContext, args: GetMetricStatsArgs) -> dict[str, Any]:
    if args.metric not in METRIC_FIELDS:
        return {"error": f"unknown metric {args.metric!r}"}

    if args.entity_type == "vm":
        entity = await inventory_service.find_vm_by_name(ctx.session, args.name)
        external_id = entity.external_id if entity else None
        display = entity.name if entity else None
    else:
        host = await _find_host(ctx.session, args.name)
        external_id = host.external_id if host else None
        display = host.name if host else None

    if external_id is None:
        return {"error": f"no {args.entity_type} matching {args.name!r}"}

    stats = await metrics_service.metric_stats(
        ctx.mongo,
        entity_type=args.entity_type,
        external_id=external_id,
        metric=args.metric,
        days=args.days,
    )
    return {
        "entity_type": args.entity_type,
        "name": display,
        "metric": args.metric,
        "window_days": args.days,
        "mean": stats.mean,
        "p95": stats.p95,
        "max": stats.max,
        "trend_per_day": stats.trend_slope_per_day,
        "days_with_data": stats.samples,
    }


async def list_anomalies(ctx: ToolContext, args: ListAnomaliesArgs) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=args.days)
    rows, total = await ml_service.list_anomalies(
        ctx.session,
        status=AnomalyStatus(args.status) if args.status else None,
        since=since,
        limit=args.limit,
        offset=0,
    )
    items = await ml_service.hydrate_anomalies(ctx.session, rows)
    return {
        "window_days": args.days,
        "total_matching": total,
        "anomalies": [
            {
                "vm": a.entity_name,
                "detected_at": a.detected_at.isoformat(timespec="minutes"),
                "window_hours": r1((a.window_end - a.window_start).total_seconds() / 3600),
                "score": r1(a.score),
                "status": str(a.status),
                "cpu_pct": r1(a.features.get("cpu_mean")),
                "disk_latency_ms": r1(a.features.get("lat_mean")),
                "summary": a.summary,
            }
            for a in items
        ],
    }


async def list_recommendations(ctx: ToolContext, args: ListRecommendationsArgs) -> dict[str, Any]:
    cluster_id: uuid.UUID | None = None
    if args.cluster_name:
        cluster = await _find_cluster(ctx.session, args.cluster_name)
        if cluster is None:
            return {"error": f"no cluster matching {args.cluster_name!r}", "recommendations": []}
        cluster_id = cluster.id

    rows, total = await ml_service.list_recommendations(
        ctx.session,
        kind=RecommendationKind(args.kind) if args.kind else None,
        status=RecommendationStatus.OPEN,
        cluster_id=cluster_id,
        limit=args.limit,
        offset=0,
    )
    items = await ml_service.hydrate_recommendations(ctx.session, rows)
    vcpus, memory_mb = await inventory_service.reclaimable_totals(
        ctx.session, cluster_id=cluster_id
    )
    return {
        "total_matching": total,
        "total_reclaimable_vcpus": vcpus,
        "total_reclaimable_memory_gb": r1(memory_mb / 1024),
        "recommendations": [
            {
                "vm": item.vm_name,
                "cluster": item.cluster_name,
                "kind": str(item.kind),
                "current": r1(item.current_value),
                "suggested": r1(item.suggested_value),
                "evidence_cpu_p95": r1(item.evidence.get("cpu_pct_p95")),
                "evidence_mem_p95": r1(item.evidence.get("mem_pct_p95")),
                "window_days": item.evidence.get("window_days"),
            }
            for item in items
        ],
    }


async def get_capacity_forecast(ctx: ToolContext, args: GetCapacityForecastArgs) -> dict[str, Any]:
    if not args.cluster_name and not args.datastore_name:
        return {"error": "provide either cluster_name or datastore_name"}

    if args.datastore_name:
        datastore = await _find_datastore(ctx.session, args.datastore_name)
        if datastore is None:
            return {"error": f"no datastore matching {args.datastore_name!r}"}
        forecasts = await ml_service.latest_forecasts(
            ctx.session, entity_type=EntityType.DATASTORE, entity_id=datastore.id
        )
        return {
            "entity_type": "datastore",
            "name": datastore.name,
            "capacity_gb": r1(datastore.capacity_gb),
            "free_gb": r1(datastore.free_gb),
            "used_pct": r1(datastore.used_pct),
            "threshold_pct": 90,
            "forecasts": _compact_forecasts(forecasts, 90.0),
        }

    cluster = await _find_cluster(ctx.session, args.cluster_name or "")
    if cluster is None:
        return {"error": f"no cluster matching {args.cluster_name!r}"}
    forecasts = await ml_service.latest_forecasts(
        ctx.session, entity_type=EntityType.CLUSTER, entity_id=cluster.id
    )
    return {
        "entity_type": "cluster",
        "name": cluster.name,
        "threshold_pct": 85,
        "forecasts": _compact_forecasts(forecasts, 85.0),
    }


def _compact_forecasts(forecasts: list[Any], threshold_pct: float) -> list[dict[str, Any]]:
    """Weekly samples only: 30 daily points per metric per model would eat the
    context window for no extra insight.

    `days_to_threshold` is restated as an explicit sentence as well as a number.
    A bare `null` is something a small model has to interpret, and measured
    against the 3B it interpreted it by inventing a figure - it reported "18
    days" for a datastore the tool said would not cross the threshold at all.
    Saying so in words costs a dozen tokens and removes the guess.
    """
    out = []
    for item in forecasts:
        points = item.points
        sampled = [points[i] for i in (0, 6, 13, 20, 29) if i < len(points)]
        days = item.days_to_threshold
        out.append(
            {
                "metric": item.metric,
                "model": str(item.model),
                "days_to_threshold": days,
                "verdict": (
                    f"crosses {threshold_pct:.0f}% in about {days} days"
                    if days is not None
                    else f"does NOT reach {threshold_pct:.0f}% within the next 30 days"
                ),
                "series": [{"date": str(p.date), "value": r1(p.value)} for p in sampled],
            }
        )
    return out


# --- Registry -------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Any


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_inventory_summary",
        "Counts and CPU/memory allocation for the whole estate, or for one cluster. "
        "Use this for 'how many', 'how big', or overcommit questions.",
        GetInventorySummaryArgs,
        get_inventory_summary,
    ),
    ToolSpec(
        "search_vms",
        "Find virtual machines by name fragment, cluster, power state or vCPU count. "
        "Returns a compact list, at most 20.",
        SearchVMsArgs,
        search_vms,
    ),
    ToolSpec(
        "get_vm_details",
        "Full detail for one VM by name: specs, latest metrics, 14-day CPU/memory "
        "statistics, open anomalies and open recommendations.",
        GetVMDetailsArgs,
        get_vm_details,
    ),
    ToolSpec(
        "get_metric_stats",
        "mean, p95, max and trend for one metric on one VM or host over N days "
        "(max 90). Use for 'how busy is X' questions.",
        GetMetricStatsArgs,
        get_metric_stats,
    ),
    ToolSpec(
        "list_anomalies",
        "Recently detected anomalies, newest first, optionally filtered by status.",
        ListAnomaliesArgs,
        list_anomalies,
    ),
    ToolSpec(
        "list_recommendations",
        "Open right-sizing recommendations with the totals they would reclaim. "
        "Use kind='idle' for idle VMs, 'downsize_cpu'/'downsize_mem' for "
        "over-provisioned ones, 'upsize_cpu'/'upsize_mem' for constrained ones.",
        ListRecommendationsArgs,
        list_recommendations,
    ),
    ToolSpec(
        "get_capacity_forecast",
        "30-day capacity forecast and days-to-threshold for one cluster or one "
        "datastore. Use for 'when will X fill up / run out' questions.",
        GetCapacityForecastArgs,
        get_capacity_forecast,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def openai_tool_schemas() -> list[dict[str, Any]]:
    """Tool definitions in the OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args_model.model_json_schema(),
            },
        }
        for spec in TOOLS
    ]


class ToolExecutionError(Exception):
    """Raised for an unknown tool or invalid arguments; fed back to the model once."""


async def execute(ctx: ToolContext, name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise ToolExecutionError(
            f"Unknown tool {name!r}. Available tools: {', '.join(TOOLS_BY_NAME)}"
        )
    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        raise ToolExecutionError(
            f"Invalid arguments for {name}: {exc.errors(include_url=False)}"
        ) from exc

    # Not "args": that is a reserved LogRecord attribute and logging raises
    # KeyError rather than shadowing it.
    log.info("assistant tool call", extra={"tool": name, "tool_args": args.model_dump()})
    return await spec.handler(ctx, args)
