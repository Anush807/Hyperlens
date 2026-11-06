"""Rule-based right-sizing (spec 9.4).

Deliberately rules, not a model: an operator has to act on these, and
"p95 CPU was 12% for 14 days" is an argument a human can check. Every rule
records the numbers that fired it in `evidence`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

WINDOW_DAYS = 14

IDLE_CPU_P95 = 5.0
IDLE_NET_KBPS = 10.0
DOWNSIZE_CPU_P95 = 20.0
DOWNSIZE_CPU_MIN_VCPUS = 2
DOWNSIZE_CPU_TARGET_UTIL = 50.0
DOWNSIZE_MEM_P95 = 30.0
DOWNSIZE_MEM_MIN_MB = 4096
DOWNSIZE_MEM_TARGET_UTIL = 60.0
UPSIZE_CPU_P95 = 85.0
UPSIZE_MEM_P95 = 90.0
UPSIZE_MIN_DAYS = 5
UPSIZE_FACTOR = 1.5

MIN_DAYS_REQUIRED = 7  # a week of evidence before recommending anything


@dataclass(frozen=True)
class VMSpec:
    external_id: str
    name: str
    vcpus: int
    memory_mb: int
    power_state: str


@dataclass(frozen=True)
class Suggestion:
    external_id: str
    kind: str
    current_value: float
    suggested_value: float
    evidence: dict[str, Any]


def _round_up_to_gb(megabytes: float) -> int:
    return int(math.ceil(megabytes / 1024.0) * 1024)


def _p95_series(window: pd.DataFrame, column: str) -> np.ndarray:
    if column not in window.columns:
        return np.array([])
    return window[column].dropna().to_numpy(dtype=np.float64)


def evaluate_vm(spec: VMSpec, window: pd.DataFrame) -> list[Suggestion]:
    """Apply every rule to one VM's last-14-days daily frame.

    `idle` short-circuits: recommending a CPU downsize on a VM you are about to
    decommission is noise.
    """
    if spec.power_state != "on":
        return []

    cpu_p95 = _p95_series(window, "cpu_pct_p95")
    mem_p95 = _p95_series(window, "mem_pct_p95")
    net_mean = _p95_series(window, "net_kbps_mean")
    if cpu_p95.size < MIN_DAYS_REQUIRED:
        return []

    cpu_typical = float(np.percentile(cpu_p95, 95)) if cpu_p95.size else 0.0
    mem_typical = float(np.percentile(mem_p95, 95)) if mem_p95.size else 0.0
    net_typical = float(np.percentile(net_mean, 95)) if net_mean.size else 0.0
    days = int(cpu_p95.size)

    base_evidence = {
        "window_days": days,
        "cpu_pct_p95": round(cpu_typical, 2),
        "mem_pct_p95": round(mem_typical, 2),
        "net_kbps_p95": round(net_typical, 2),
        "vcpus": spec.vcpus,
        "memory_mb": spec.memory_mb,
    }

    if cpu_typical < IDLE_CPU_P95 and net_typical < IDLE_NET_KBPS:
        return [
            Suggestion(
                external_id=spec.external_id,
                kind="idle",
                current_value=float(spec.vcpus),
                suggested_value=0.0,
                evidence={
                    **base_evidence,
                    "rule": f"p95 CPU < {IDLE_CPU_P95}% and p95 net < {IDLE_NET_KBPS} kbps",
                },
            )
        ]

    suggestions: list[Suggestion] = []

    if cpu_typical < DOWNSIZE_CPU_P95 and spec.vcpus > DOWNSIZE_CPU_MIN_VCPUS:
        target = max(
            DOWNSIZE_CPU_MIN_VCPUS,
            math.ceil(spec.vcpus * cpu_typical / DOWNSIZE_CPU_TARGET_UTIL),
        )
        if target < spec.vcpus:
            suggestions.append(
                Suggestion(
                    external_id=spec.external_id,
                    kind="downsize_cpu",
                    current_value=float(spec.vcpus),
                    suggested_value=float(target),
                    evidence={
                        **base_evidence,
                        "rule": (
                            f"p95 CPU < {DOWNSIZE_CPU_P95}% with more than "
                            f"{DOWNSIZE_CPU_MIN_VCPUS} vCPUs"
                        ),
                        "target_utilisation_pct": DOWNSIZE_CPU_TARGET_UTIL,
                    },
                )
            )

    if mem_typical and mem_typical < DOWNSIZE_MEM_P95 and spec.memory_mb > DOWNSIZE_MEM_MIN_MB:
        target_mb = max(
            DOWNSIZE_MEM_MIN_MB,
            _round_up_to_gb(spec.memory_mb * mem_typical / DOWNSIZE_MEM_TARGET_UTIL),
        )
        if target_mb < spec.memory_mb:
            suggestions.append(
                Suggestion(
                    external_id=spec.external_id,
                    kind="downsize_mem",
                    current_value=float(spec.memory_mb),
                    suggested_value=float(target_mb),
                    evidence={
                        **base_evidence,
                        "rule": f"p95 memory < {DOWNSIZE_MEM_P95}% with > {DOWNSIZE_MEM_MIN_MB} MB",
                        "target_utilisation_pct": DOWNSIZE_MEM_TARGET_UTIL,
                    },
                )
            )

    hot_cpu_days = int((cpu_p95 > UPSIZE_CPU_P95).sum())
    if hot_cpu_days >= UPSIZE_MIN_DAYS:
        suggestions.append(
            Suggestion(
                external_id=spec.external_id,
                kind="upsize_cpu",
                current_value=float(spec.vcpus),
                suggested_value=float(math.ceil(spec.vcpus * UPSIZE_FACTOR)),
                evidence={
                    **base_evidence,
                    "rule": f"p95 CPU > {UPSIZE_CPU_P95}% on >= {UPSIZE_MIN_DAYS} of {days} days",
                    "hot_days": hot_cpu_days,
                },
            )
        )

    hot_mem_days = int((mem_p95 > UPSIZE_MEM_P95).sum()) if mem_p95.size else 0
    if hot_mem_days >= UPSIZE_MIN_DAYS:
        suggestions.append(
            Suggestion(
                external_id=spec.external_id,
                kind="upsize_mem",
                current_value=float(spec.memory_mb),
                suggested_value=float(_round_up_to_gb(spec.memory_mb * UPSIZE_FACTOR)),
                evidence={
                    **base_evidence,
                    "rule": (
                        f"p95 memory > {UPSIZE_MEM_P95}% on at least "
                        f"{UPSIZE_MIN_DAYS} of {days} days"
                    ),
                    "hot_days": hot_mem_days,
                },
            )
        )

    return suggestions


def evaluate_all(specs: list[VMSpec], daily: pd.DataFrame) -> list[Suggestion]:
    """`daily` is the long frame from ml.data.load_daily."""
    if daily.empty:
        return []
    cutoff = daily["date"].max() - pd.Timedelta(days=WINDOW_DAYS)
    recent = daily[daily["date"] > cutoff]
    grouped = {str(k): v for k, v in recent.groupby("entity_external_id")}

    out: list[Suggestion] = []
    for spec in specs:
        window = grouped.get(spec.external_id)
        if window is None or window.empty:
            continue
        out.extend(evaluate_vm(spec, window))
    return out
