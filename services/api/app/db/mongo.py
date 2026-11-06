"""Motor client plus first-run collection/index creation.

The metrics collection is a native time-series collection (spec 6.3), which can
only be created explicitly - it is not created implicitly on first insert with
the right options.
"""

from __future__ import annotations

import contextlib
import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid

from app.config import settings

log = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None

METRICS = "metrics"
METRICS_DAILY = "metrics_daily"
EVENTS = "events"


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            settings.mongo_url, tz_aware=True, uuidRepresentation="standard"
        )
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.mongo_db]


async def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def init_mongo() -> None:
    """Idempotent: safe to run on every API start."""
    db = get_db()
    existing = set(await db.list_collection_names())

    if METRICS not in existing:
        try:
            await db.create_collection(
                METRICS,
                timeseries={
                    "timeField": "ts",
                    "metaField": "meta",
                    "granularity": "minutes",
                },
                expireAfterSeconds=settings.metrics_ttl_days * 86400,
            )
            log.info("created time-series collection %s", METRICS)
        except CollectionInvalid:
            pass

    if EVENTS not in existing:
        # A concurrent API replica may have created it between the list and now.
        with contextlib.suppress(CollectionInvalid):
            await db.create_collection(EVENTS)

    await db[EVENTS].create_index(
        [("created_at", ASCENDING)], expireAfterSeconds=settings.events_ttl_days * 86400
    )
    await db[METRICS].create_index(
        [
            ("meta.entity_type", ASCENDING),
            ("meta.entity_external_id", ASCENDING),
            ("ts", DESCENDING),
        ]
    )
    await db[METRICS_DAILY].create_index(
        [("entity_type", ASCENDING), ("entity_external_id", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_entity_date",
    )
    await db[METRICS_DAILY].create_index([("date", DESCENDING)])
