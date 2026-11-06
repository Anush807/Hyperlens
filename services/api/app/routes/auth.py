"""Auth endpoints. Refresh tokens are returned in the body *and* set as an
httpOnly cookie so the SPA never has to store them in JS (spec 11.2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.config import settings
from app.deps import AdminUser, CurrentUser, DbSession, client_ip
from app.errors import AuthError
from app.schemas.auth import RefreshRequest, TokenPair, UserCreate, UserOut
from app.services import audit, auth_service

router = APIRouter(tags=["auth"])

REFRESH_COOKIE = "hvops_refresh"


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        httponly=True,
        samesite="lax",
        secure=False,  # dev runs over http; flip with a reverse proxy in production
        max_age=settings.jwt_refresh_ttl_days * 86400,
        path="/auth",
    )


@router.post("/auth/login", response_model=TokenPair)
async def login(
    request: Request,
    response: Response,
    session: DbSession,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenPair:
    user = await auth_service.authenticate(session, form.username, form.password)
    access, refresh, ttl = await auth_service.issue_tokens(session, user)
    await audit.record(
        session,
        user_id=user.id,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
        details={"ip": client_ip(request)},
    )
    await session.commit()
    _set_refresh_cookie(response, refresh)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl)


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    response: Response,
    session: DbSession,
    body: RefreshRequest | None = None,
    hvops_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenPair:
    raw = (body.refresh_token if body else None) or hvops_refresh
    if not raw:
        raise AuthError("No refresh token supplied")
    user, access, new_raw, ttl = await auth_service.rotate_refresh_token(session, raw)
    await session.commit()
    _set_refresh_cookie(response, new_raw)
    return TokenPair(access_token=access, refresh_token=new_raw, expires_in=ttl)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session: DbSession,
    user: CurrentUser,
    body: RefreshRequest | None = None,
    hvops_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> Response:
    raw = (body.refresh_token if body else None) or hvops_refresh
    if raw:
        await auth_service.revoke_refresh_token(session, raw)
    await audit.record(
        session, user_id=user.id, action="auth.logout", entity_type="user", entity_id=user.id
    )
    await session.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(session: DbSession, admin: AdminUser, payload: UserCreate) -> UserOut:
    user = await auth_service.create_user(session, payload.email, payload.password, payload.role)
    await audit.record(
        session,
        user_id=admin.id,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        details={"email": user.email, "role": str(user.role)},
    )
    await session.commit()
    return UserOut.model_validate(user)
