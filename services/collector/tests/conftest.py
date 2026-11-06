from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.schemas import SourceConfig


@pytest.fixture
def vsphere_source() -> SourceConfig:
    return SourceConfig(
        id=uuid.uuid4(),
        name="vcsim-lab",
        type="vsphere",
        endpoint="vcsim:8989",
        username="user",
        password="pass",
        verify_ssl=False,
    )


@pytest.fixture
def nutanix_source() -> SourceConfig:
    return SourceConfig(
        id=uuid.uuid4(),
        name="nutanix-mock",
        type="nutanix",
        endpoint="http://nutanix-mock:9440",
        username="admin",
        password="admin",
        verify_ssl=False,
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
