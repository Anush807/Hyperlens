from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import Role
from app.schemas.common import Email


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    # Optional in the body: the browser normally sends it as an httpOnly cookie.
    refresh_token: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: Email
    role: Role
    is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)
    role: Role = Role.VIEWER
