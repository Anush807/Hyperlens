"""`{items, total, limit, offset}` envelope shared by every list endpoint."""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class PageParams(BaseModel):
    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)


def page_params(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)
