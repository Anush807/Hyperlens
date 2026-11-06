from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import MessageRole


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    tool_name: str | None
    tool_args: dict[str, Any] | None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    messages: list[MessageOut] = []


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
