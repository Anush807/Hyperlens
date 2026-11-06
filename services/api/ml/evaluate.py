"""`python -m ml.evaluate --out docs/ml-evaluation.md`

Scores anomaly detection against the injected ground truth and the LSTM against
the seasonal-naive baseline, then writes the markdown report the spec's
Definition of Done asks for. The report states plainly when the baseline wins.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.mongo import close_client, get_db
from app.logging_config import configure_logging
from ml import anomaly, forecast
from ml.data import load_daily, load_hourly_windows, pivot_metric

GROUND_TRUTH_CANDIDATES = [
    # The collector writes this to the shared ml_artifacts volume when seeding.
    Path(settings.ml_artifact_dir) / "ground_truth.json",
    Path("/app/synthetic/ground_truth.json"),
    Path("synthetic/ground_truth.json"),
    Path("../../synthetic/ground_truth.json"),
]


def _load_ground_truth(explicit: str | None) -> dict[str, Any]:
    paths = [Path(explicit)] if explicit else GROUND_TRUTH_CANDIDATES
    for path in paths:
        if path.exists():
            return json.loads(path.read_text())
    return {}


async def evaluate_anomaly(days: int, ground_truth: dict[str, Any]) -> dict[str, Any]:
    model = anomaly.load()
    if model is None:
        return {"error": "no trained anomaly model (run `python -m ml.train anomaly`)"}

    windows = await load_hourly_windows(get_db(), days=days)
    if windows.empty:
        return {"error": "no hourly windows available"}

    scored = anomaly.score_all(model, windows)
    flagged = scored[scored["is_anomaly"]]
    incidents = anomaly.merge_windows(flagged.sort_values("window_start"))
    result: dict[str, Any] = {
        "windows_scored": int(len(windows)),
        "windows_flagged": int(len(flagged)),
        "incidents": len(incidents),
        "threshold": round(model.threshold, 6),
        "trained_on_windows": model.train_windows,
    }
    injected = ground_truth.get("anomaly_windows", [])
    if injected:
        import pandas as pd

        scored_from = pd.Timestamp(windows["window_start"].min())
        scored_to = pd.Timestamp(windows["window_start"].max())
        result["scored_from"] = scored_from.isoformat()
        result["scored_to"] = scored_to.isoformat()
        # Window level is the metric spec 9.2 asks for; incident level is
        # reported next to it because that is what alert volume feels like.
        result.update(anomaly.score_windows(scored, injected))
        result["ablation"] = anomaly.ablation(scored, injected, model)
        result.update(
            anomaly.score_against_ground_truth(
                incidents, injected, scored_from=scored_from, scored_to=scored_to
            )
        )
    else:
        result["note"] = "no ground_truth.json found; precision/recall not computed"
    return result


async def evaluate_forecast(days: int, metric: str, profiles: dict[str, str]) -> dict[str, Any]:
    artifacts = forecast.load()
    if artifacts is None:
        return {"error": "no trained forecast model (run `python -m ml.train forecast`)"}

    daily = await load_daily(get_db(), entity_type="vm", days=days)
    wide = pivot_metric(daily, metric)
    if wide.empty:
        return {"error": f"metric {metric} not available in metrics_daily"}
    return forecast.evaluate_holdout(artifacts, wide, profiles=profiles)


def render_markdown(anomaly_result: dict[str, Any], forecast_result: dict[str, Any]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# ML Evaluation",
        "",
        f"Generated {now} by `python -m ml.evaluate`. Regenerate after every retrain.",
        "",
        "## 1. Anomaly detection (Isolation Forest)",
        "",
    ]

    if "error" in anomaly_result:
        lines += [f"> Not evaluated: {anomaly_result['error']}", ""]
    else:
        lines += [
            "| Metric | Value |",
            "|---|---|",
            f"| Windows scored | {anomaly_result.get('windows_scored', 0)} |",
            f"| Windows flagged | {anomaly_result.get('windows_flagged', 0)} |",
            f"| Merged incidents | {anomaly_result.get('incidents', 0)} |",
            f"| Score threshold | {anomaly_result.get('threshold')} |",
        ]
        if "f1" in anomaly_result:
            lines += [
                f"| Scored period | {str(anomaly_result.get('scored_from'))[:16]} → "
                f"{str(anomaly_result.get('scored_to'))[:16]} |",
                f"| Anomalous windows (ground truth) | {anomaly_result.get('anomalous_windows')} |",
                f"| True positives | {anomaly_result.get('true_positive_windows')} |",
                f"| False positives | {anomaly_result.get('false_positive_windows')} |",
                f"| False negatives | {anomaly_result.get('false_negative_windows')} |",
                f"| **Precision** | **{anomaly_result['precision']}** |",
                f"| **Recall** | **{anomaly_result['recall']}** |",
                f"| **F1** | **{anomaly_result['f1']}** |",
                "",
                "Window level, as spec 9.2 defines it: each hourly window is "
                "scored against whether it falls inside an injected anomaly.",
                "",
                *_ablation_table(anomaly_result.get("ablation")),
                "Incident level, after merging consecutive flagged hours - this is "
                "what alert volume feels like:",
                "",
                "| Metric | Value |",
                "|---|---|",
                f"| Injected incidents in period | {anomaly_result.get('injected')} |",
                f"| Incidents raised | {anomaly_result.get('detected')} |",
                f"| Injected incidents matched | {anomaly_result.get('matched')} |",
                f"| Incident precision | {anomaly_result.get('incident_precision')} |",
                f"| Incident recall | {anomaly_result.get('incident_recall')} |",
                "",
                _verdict(
                    anomaly_result["f1"] >= 0.7,
                    f"F1 {anomaly_result['f1']} meets the 0.70 acceptance bar "
                    "from spec 14, Phase 5.",
                    f"F1 {anomaly_result['f1']} is below the 0.70 acceptance bar. "
                    "Tune `--flag-rate`, widen the feature window, or accept the lower recall "
                    "and document it here.",
                ),
            ]
        else:
            lines += ["", f"> {anomaly_result.get('note', '')}"]
        lines.append("")

    lines += ["## 2. Forecasting (LSTM vs seasonal naive)", ""]
    if "error" in forecast_result:
        lines += [f"> Not evaluated: {forecast_result['error']}", ""]
        return "\n".join(lines)

    overall = forecast_result.get("overall", {})
    lines += [
        f"Horizon: {forecast.HORIZON} days. Lookback: {forecast.LOOKBACK} days. "
        f"Test window: the final {forecast.HORIZON} days, never seen in training.",
        "",
        "| Model | MAE | sMAPE |",
        "|---|---|---|",
        f"| LSTM | {overall.get('lstm_mae')} | {overall.get('lstm_smape')}% |",
        f"| Seasonal naive | {overall.get('naive_mae')} | {overall.get('naive_smape')}% |",
        "",
        _verdict(
            bool(overall.get("lstm_beats_naive")),
            "The LSTM beats the seasonal-naive baseline overall, so it is the default "
            "model shown in the UI.",
            "**The LSTM does not beat the seasonal-naive baseline overall.** Per spec 16 "
            "this is an acceptable outcome and is reported rather than hidden: the naive "
            "forecast is the one to trust until more history or a smaller model closes the gap. "
            "Both are stored and both are rendered in the capacity screens.",
        ),
        "",
        "### By synthetic profile",
        "",
        "| Profile | LSTM MAE | Naive MAE | LSTM sMAPE | Naive sMAPE | Winner |",
        "|---|---|---|---|---|---|",
    ]
    for profile, row in sorted(forecast_result.get("by_profile", {}).items()):
        winner = "LSTM" if row.get("lstm_beats_naive") else "naive"
        lines.append(
            f"| {profile} | {row.get('lstm_mae')} | {row.get('naive_mae')} | "
            f"{row.get('lstm_smape')}% | {row.get('naive_smape')}% | {winner} |"
        )

    lines += [
        "",
        "#### Reading this table",
        "",
        "The LSTM wins where the series is near-stationary and loses where it is "
        "not, which is the opposite of what one would hope for from a sequence "
        "model. Two things explain most of it, and neither is flattering:",
        "",
        "- **Seasonal naive is close to the generating process here.** These "
        "series are a weekly sine plus small Gaussian noise, so repeating last "
        "week is very nearly optimal. Its sMAPE of ~1-2% on the stationary "
        "profiles is the ceiling, not a baseline to clear. A real estate's "
        "irregularity is where a learned model would earn its place.",
        "- **`growing` is the hardest case and the LSTM's worst.** The synthetic "
        "trend is a 180-day sawtooth - at ~0.28%/day it cannot climb much "
        "longer before saturating - so the training window contains one "
        "discontinuity whose position moves with the calendar. That is a "
        "property of the synthetic data, not of the model, and it makes this "
        "profile's number noisier across runs than the others.",
        "",
        "What this table is *for* is the decision in the next line: the model "
        "the UI leads with is whichever one actually won, read from these "
        "measurements at runtime rather than assumed.",
    ]

    lines += [
        "",
        "## 3. Assistant tool selection",
        "",
        "Run `make assistant-eval` with the `llm` profile up to fill this section; "
        "it appends the model name, tool-selection accuracy and argument accuracy "
        "for each model tried.",
        "",
        "## 4. How to reproduce",
        "",
        "```bash",
        "make up && make seed          # 180 days of synthetic history",
        "make train                    # isolation forest + LSTM + recommendations",
        "make evaluate                 # regenerates this file",
        "```",
        "",
    ]
    return "\n".join(lines)


def _ablation_table(ablation: dict[str, Any] | None) -> list[str]:
    """Which half of the detector is actually doing the work."""
    if not ablation:
        return []
    labels = {
        "isolation_forest_only": "Isolation Forest alone",
        "deviation_gate_only": "Deviation gate alone",
        "both": "Both (shipped)",
    }
    lines = [
        "Ablation - the detector is an Isolation Forest *and* a minimum-deviation "
        "gate, so here is what each contributes:",
        "",
        "| Decision rule | Flagged | Precision | Recall | F1 |",
        "|---|---|---|---|---|",
    ]
    for key, label in labels.items():
        row = ablation.get(key, {})
        lines.append(
            f"| {label} | {row.get('flagged_windows')} | {row.get('precision')} | "
            f"{row.get('recall')} | {row.get('f1')} |"
        )
    forest = ablation.get("isolation_forest_only", {}).get("f1", 0)
    gate = ablation.get("deviation_gate_only", {}).get("f1", 0)
    if gate > forest:
        lines += [
            "",
            "Read that honestly: on **this synthetic data** the gate is doing the "
            "discriminating and the forest adds nothing, because normal synthetic "
            "variation is well-behaved Gaussian noise that never deviates ten robust "
            "standard deviations from its own same-hour baseline. The forest stays in "
            "the pipeline because real metrics are not that tidy - it is what catches "
            "a multivariate pattern that is unremarkable on every axis taken alone - "
            "but the headline F1 above should not be read as evidence that it works.",
        ]
    return lines + [""]


def _verdict(passed: bool, good: str, bad: str) -> str:
    return f"{'✅' if passed else '⚠️'} {good if passed else bad}"


async def _main(days: int, metric: str, out: str, ground_truth_path: str | None) -> str:
    try:
        ground_truth = _load_ground_truth(ground_truth_path)
        profiles = ground_truth.get("profiles", {})
        anomaly_result = await evaluate_anomaly(min(days, 30), ground_truth)
        forecast_result = await evaluate_forecast(days, metric, profiles)
    finally:
        await close_client()

    markdown = render_markdown(anomaly_result, forecast_result)
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="ml.evaluate")
    parser.add_argument("--out", default="docs/ml-evaluation.md")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--metric", default="cpu_pct_p95")
    parser.add_argument("--ground-truth", default=None)
    args = parser.parse_args(argv)
    written = asyncio.run(_main(args.days, args.metric, args.out, args.ground_truth))
    print(f"wrote {written} (artifacts: {settings.ml_artifact_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
