from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import SourceStatus, SourceType


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: SourceType
    endpoint: str
    username: str
    verify_ssl: bool
    is_enabled: bool
    last_inventory_at: datetime | None
    last_status: SourceStatus
    last_error: str | None


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: SourceType
    endpoint: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    verify_ssl: bool = False
    is_enabled: bool = True


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    endpoint: str | None = Field(default=None, min_length=1, max_length=512)
    username: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    is_enabled: bool | None = None


class SyncResult(BaseModel):
    triggered: bool
    detail: str
