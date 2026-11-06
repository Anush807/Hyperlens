"""API test fixtures.

The suite needs a real Postgres and a real Mongo: the ingest path leans on
`ON CONFLICT` upserts and on native time-series collections, and neither
survives being mocked.

It gets them by creating a throwaway database on whatever server DATABASE_URL /
MONGO_URL already point at - which is the compose stack when you run
`make test-api`, and a testcontainers-provided server in CI (set the two URLs
and the suite does not care which it is). Everything is created and dropped per
session, so a test run can never touch real inventory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# --- environment, before any app module is imported -----------------------

_BASE_DSN = os.environ.get("DATABASE_URL", "postgresql+asyncpg://hvops:hvops@postgres:5432/hvops")
_TEST_DB = f"hvops_test_{uuid.uuid4().hex[:8]}"


def _with_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


os.environ["DATABASE_URL"] = _with_database(_BASE_DSN, _TEST_DB)
os.environ["MONGO_DB"] = f"hvops_test_{uuid.uuid4().hex[:8]}"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["TESTING"] = "true"
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-service-token")


def _admin_dsn() -> str:
    """asyncpg cannot CREATE DATABASE from inside the target database."""
    return _with_database(_BASE_DSN, "postgres").replace("postgresql+asyncpg", "postgresql")


async def _create_database() -> None:
    import asyncpg

    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    finally:
        await conn.close()


async def _drop_database() -> None:
    import asyncpg

    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", _TEST_DB
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
    finally:
        await conn.close()


async def _create_schema() -> None:
    from sqlalchemy import text

    from app.db import models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.mongo import close_client, init_mongo
    from app.db.postgres import Base, engine

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)
    await init_mongo()
    await close_client()
    await engine.dispose()


# Setup runs in pytest's own hooks rather than in a session-scoped async
# fixture: a session-scoped fixture gets its own event loop, and the asyncpg
# connections it opens cannot then be used from a per-test loop.
def pytest_configure(config):  # noqa: ARG001
    asyncio.run(_create_database())
    asyncio.run(_create_schema())


def pytest_unconfigure(config):  # noqa: ARG001
    # Teardown must never turn a green run red.
    with contextlib.suppress(Exception):
        asyncio.run(_drop_database())


@pytest_asyncio.fixture(autouse=True)
async def clean_db() -> AsyncIterator[None]:
    """Every test starts from an empty database; ordering never matters.

    The Motor client is dropped around each test as well. It is a module-level
    singleton that binds to whatever event loop first used it, and each test
    gets its own loop - reusing it raises "attached to a different loop".
    """
    from sqlalchemy import text

    from app.cache import invalidate
    from app.db.mongo import close_client
    from app.db.postgres import SessionLocal

    invalidate()
    await close_client()
    async with SessionLocal() as s:
        await s.execute(
            text(
                "TRUNCATE users, refresh_tokens, hypervisor_sources, clusters, hosts, vms, "
                "datastores, anomalies, forecasts, recommendations, assistant_conversations, "
                "assistant_messages, audit_log RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()
    yield

    from app.db.mongo import METRICS, METRICS_DAILY, get_db

    db = get_db()
    await db[METRICS].delete_many({})
    await db[METRICS_DAILY].delete_many({})
    await close_client()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:  # noqa: F821
    from app.db.postgres import SessionLocal

    async with SessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def mongo() -> AsyncIterator[AsyncIOMotorDatabase]:  # noqa: F821
    from app.db.mongo import get_db

    yield get_db()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """The real app without its lifespan: no scheduler, no admin seeding."""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --- users ----------------------------------------------------------------


def auth_header(user) -> dict[str, str]:
    from app.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user.id, str(user.role), user.email)}"}


async def _make_user(email: str, password: str, role):
    from app.db.postgres import SessionLocal
    from app.services.auth_service import create_user

    async with SessionLocal() as s:
        user = await create_user(s, email, password, role)
        await s.commit()
        await s.refresh(user)
        return user


@pytest_asyncio.fixture
async def admin():
    from app.db.models.enums import Role

    return await _make_user("admin@hvops.local", "admin-password", Role.ADMIN)


@pytest_asyncio.fixture
async def operator():
    from app.db.models.enums import Role

    return await _make_user("operator@hvops.local", "operator-password", Role.OPERATOR)


@pytest_asyncio.fixture
async def viewer():
    from app.db.models.enums import Role

    return await _make_user("viewer@hvops.local", "viewer-password", Role.VIEWER)


@pytest_asyncio.fixture
async def source():
    from app.db.postgres import SessionLocal
    from app.schemas.sources import SourceCreate
    from app.services.source_service import create_source

    async with SessionLocal() as s:
        created = await create_source(
            s,
            SourceCreate(
                name="vcsim-lab",
                type="vsphere",
                endpoint="vcsim:8989",
                username="user",
                password="pass",
            ),
        )
        await s.commit()
        await s.refresh(created)
        return created


@pytest.fixture
def service_header() -> dict[str, str]:
    from app.config import settings

    return {"X-Service-Token": settings.internal_service_token}


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def yesterday(now) -> datetime:
    return now - timedelta(days=1)
