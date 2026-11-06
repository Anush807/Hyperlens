"""Shared FastAPI dependencies: sessions, current user, role guards, service token."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorDatabase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import User
from app.db.models.enums import ROLE_ORDER, Role
from app.db.mongo import get_db
from app.db.postgres import get_session
from app.errors import AuthError, ForbiddenError
from app.security import constant_time_equals, decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def mongo_db() -> AsyncIterator[AsyncIOMotorDatabase]:
    yield get_db()


MongoDb = Annotated[AsyncIOMotorDatabase, Depends(mongo_db)]


async def get_current_user(
    session: DbSession,
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> User:
    if not token:
        raise AuthError("Missing bearer token")
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Access token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid access token") from exc

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError("Malformed token subject") from exc

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User not found or disabled")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: str):
    """Dependency factory. admin > operator > viewer, compared by position."""
    if minimum not in ROLE_ORDER:
        raise ValueError(f"unknown role {minimum!r}")
    needed = ROLE_ORDER.index(minimum)

    async def _guard(user: CurrentUser) -> User:
        if ROLE_ORDER.index(user.role) < needed:
            raise ForbiddenError(f"Requires {minimum} role or higher")
        return user

    return _guard


ViewerUser = Annotated[User, Depends(require_role(Role.VIEWER))]
OperatorUser = Annotated[User, Depends(require_role(Role.OPERATOR))]
AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]


async def require_service_token(
    x_service_token: Annotated[str | None, Header(alias="X-Service-Token")] = None,
) -> None:
    """Guards /internal/*. Deliberately not JWT: the collector holds no user identity."""
    if not x_service_token or not constant_time_equals(
        x_service_token, settings.internal_service_token
    ):
        raise AuthError("Invalid service token")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
