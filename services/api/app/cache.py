"""Tiny in-process TTL cache for the dashboard summary endpoint (spec 8.1).

Deliberately not Redis: one API replica in V1, and a cold cache costs one query.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

_store: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def cached(key: str, ttl_seconds: float, producer: Callable[[], Awaitable[Any]]) -> Any:
    now = time.monotonic()
    hit = _store.get(key)
    if hit and hit[0] > now:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _store.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await producer()
        _store[key] = (time.monotonic() + ttl_seconds, value)
        return value


def invalidate(prefix: str = "") -> None:
    for key in [k for k in _store if k.startswith(prefix)]:
        _store.pop(key, None)
