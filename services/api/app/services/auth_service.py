"""Login, refresh rotation, logout and user creation."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import RefreshToken, User
from app.db.models.enums import Role
from app.errors import AuthError, ConflictError
from app.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

log = logging.getLogger(__name__)


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    user = (
        await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    ).scalar_one_or_none()
    # Hash a dummy on miss so a missing user and a wrong password cost the same.
    if user is None:
        verify_password(password, hash_password("dummy-timing-equalizer"))
        raise AuthError("Invalid email or password")
    if not user.is_active:
        raise AuthError("User is disabled")
    if not verify_password(password, user.password_hash):
        raise AuthError("Invalid email or password")
    return user


async def issue_tokens(session: AsyncSession, user: User) -> tuple[str, str, int]:
    raw, token_hash, expires = new_refresh_token()
    session.add(RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires))
    access = create_access_token(user.id, str(user.role), user.email)
    return access, raw, settings.jwt_access_ttl_min * 60


async def rotate_refresh_token(session: AsyncSession, raw_token: str) -> tuple[User, str, str, int]:
    """Single-use refresh tokens: the presented one is revoked and replaced."""
    token_hash = hash_refresh_token(raw_token)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None:
        raise AuthError("Unknown refresh token")
    if row.revoked:
        # Replay of an already-rotated token: treat the whole family as
        # compromised. Committed here rather than left to the caller, because
        # the caller's next move is to raise - and a rollback would undo the
        # revocation, leaving the stolen token's successor usable.
        revoked = await revoke_all_for_user(session, row.user_id)
        await session.commit()
        log.warning(
            "refresh token replay detected; revoked the token family",
            extra={"user_id": str(row.user_id), "revoked": revoked},
        )
        raise AuthError("Refresh token already used")
    if row.expires_at <= datetime.now(UTC):
        raise AuthError("Refresh token expired")

    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User not found or disabled")

    row.revoked = True
    access, new_raw, ttl = await issue_tokens(session, user)
    return user, access, new_raw, ttl


async def revoke_refresh_token(session: AsyncSession, raw_token: str) -> bool:
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.revoked = True
    return True


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    rows = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked = True
    return len(rows)


async def create_user(session: AsyncSession, email: str, password: str, role: Role) -> User:
    exists = (
        await session.execute(select(User.id).where(func.lower(User.email) == email.lower()))
    ).scalar_one_or_none()
    if exists:
        raise ConflictError(f"User {email} already exists")
    user = User(email=email.lower(), password_hash=hash_password(password), role=role)
    session.add(user)
    await session.flush()
    return user


async def seed_admin_if_empty(session: AsyncSession) -> User | None:
    """Runs on startup. A fresh database would otherwise be unusable."""
    count = (await session.execute(select(func.count(User.id)))).scalar_one()
    if count:
        return None
    user = User(
        email=settings.seed_admin_email.lower(),
        password_hash=hash_password(settings.seed_admin_password),
        role=Role.ADMIN,
    )
    session.add(user)
    await session.commit()
    log.warning(
        "seeded initial admin account - change the password",
        extra={"email": settings.seed_admin_email},
    )
    return user
