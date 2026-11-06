"""`python -m ml.run_all` - detection, forecasting and right-sizing in one pass.

This is exactly what POST /ml/run schedules, so the CLI and the API cannot drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db.mongo import close_client, get_db
from app.db.postgres import SessionLocal, engine
from app.logging_config import configure_logging
from ml.jobs import run_all


async def _main(anomaly_days: int, history_days: int) -> dict[str, object]:
    try:
        async with SessionLocal() as session:
            return await run_all(
                session, get_db(), anomaly_days=anomaly_days, history_days=history_days
            )
    finally:
        await close_client()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="ml.run_all")
    parser.add_argument("--anomaly-days", type=int, default=7)
    parser.add_argument("--history-days", type=int, default=180)
    args = parser.parse_args(argv)
    print(
        json.dumps(asyncio.run(_main(args.anomaly_days, args.history_days)), indent=2, default=str)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
