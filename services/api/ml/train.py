"""Training CLI.

python -m ml.train anomaly  --days 30
python -m ml.train forecast --days 180
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.db.mongo import close_client, get_db
from app.logging_config import configure_logging
from ml import anomaly, forecast
from ml.data import load_daily, load_hourly_windows, pivot_metric

log = logging.getLogger("ml.train")


async def train_anomaly(days: int, flag_rate: float, min_deviation: float) -> dict[str, object]:
    mongo = get_db()
    windows = await load_hourly_windows(mongo, days=days)
    if windows.empty:
        raise SystemExit("no hourly metric windows found - run `make seed` first")

    model = anomaly.train(windows, flag_rate=flag_rate, min_deviation=min_deviation)
    directory = anomaly.save(model)
    flagged = anomaly.detect(model, windows)
    incidents = anomaly.merge_windows(flagged)
    return {
        "artifacts": str(directory),
        "train_windows": int(len(windows)),
        "threshold": round(model.threshold, 6),
        "min_deviation": model.min_deviation,
        "flagged_windows": int(len(flagged)),
        "incidents_on_training_data": len(incidents),
    }


async def train_forecast(days: int, metric: str, version: int) -> dict[str, object]:
    mongo = get_db()
    daily = await load_daily(mongo, entity_type="vm", days=days)
    if daily.empty:
        raise SystemExit("no daily metrics found - run `make seed` then the rollup job")

    wide = pivot_metric(daily, metric)
    if wide.empty:
        raise SystemExit(f"metric {metric!r} is not present in metrics_daily")

    artifacts = forecast.train_lstm(wide, metric_name=metric)
    evaluation = forecast.evaluate_holdout(artifacts, wide)
    artifacts.metrics["holdout"] = evaluation.get("overall", {})
    artifacts.metrics["by_profile"] = evaluation.get("by_profile", {})
    directory = forecast.save(artifacts, version=version)

    return {
        "artifacts": str(directory),
        "entities": int(wide.shape[1]),
        "days": int(wide.shape[0]),
        "metric": metric,
        **artifacts.metrics.get("holdout", {}),
    }


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="ml.train", description="Train HVOps models")
    subparsers = parser.add_subparsers(dest="command", required=True)

    anomaly_parser = subparsers.add_parser("anomaly", help="Train the Isolation Forest")
    anomaly_parser.add_argument("--days", type=int, default=30)
    anomaly_parser.add_argument(
        "--flag-rate",
        type=float,
        default=anomaly.DEFAULT_FLAG_RATE,
        help="Share of training windows allowed to be flagged (sets the score cutoff)",
    )
    anomaly_parser.add_argument(
        "--min-deviation",
        type=float,
        default=anomaly.MIN_DEVIATION,
        help="Robust standard deviations a flagged window must also reach",
    )

    forecast_parser = subparsers.add_parser("forecast", help="Train the LSTM forecaster")
    forecast_parser.add_argument("--days", type=int, default=180)
    forecast_parser.add_argument("--metric", default="cpu_pct_p95")
    forecast_parser.add_argument("--version", type=int, default=1)

    args = parser.parse_args(argv)

    async def _run() -> dict[str, object]:
        try:
            if args.command == "anomaly":
                return await train_anomaly(args.days, args.flag_rate, args.min_deviation)
            return await train_forecast(args.days, args.metric, args.version)
        finally:
            await close_client()

    result = asyncio.run(_run())
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
