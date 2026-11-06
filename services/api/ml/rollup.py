"""`python -m ml.rollup --days 180`

The API's hourly scheduler only rolls up the last 2 days, which is all a live
system needs. After a backfill the whole history has to be aggregated once -
that is what this CLI is for, and `make seed` runs it automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from app.db.mongo import METRICS_DAILY, close_client, get_db
from app.logging_config import configure_logging
from app.services.metrics_service import rollup_daily


async def _main(days: int) -> dict[str, object]:
    started = time.perf_counter()
    try:
        mongo = get_db()
        await rollup_daily(mongo, days=days)
        return {
            "days": days,
            "daily_rows": await mongo[METRICS_DAILY].count_documents({}),
            "elapsed_seconds": round(time.perf_counter() - started, 1),
        }
    finally:
        await close_client()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="ml.rollup")
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_main(args.days)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
