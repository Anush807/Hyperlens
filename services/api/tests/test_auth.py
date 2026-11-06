"""Auth flows: login, refresh rotation, revocation and role guards (spec 8.1)."""

from __future__ import annotations

import pytest

from tests.conftest import auth_header


async def login(client, email: str, password: str):
    return await client.post("/auth/login", data={"username": email, "password": password})


async def test_login_returns_both_tokens_and_sets_the_cookie(client, admin):
    response = await login(client, "admin@hvops.local", "admin-password")

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] > 0
    # The SPA relies on the httpOnly cookie, not on storing the refresh token.
    assert "hvops_refresh" in response.cookies


async def test_login_is_case_insensitive_on_email(client, admin):
    assert (await login(client, "ADMIN@HVOPS.LOCAL", "admin-password")).status_code == 200


@pytest.mark.parametrize(
    ("email", "password"),
    [("admin@hvops.local", "wrong"), ("nobody@hvops.local", "admin-password")],
)
async def test_bad_credentials_are_indistinguishable(client, admin, email, password):
    response = await login(client, email, password)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid email or password"


async def test_disabled_user_cannot_log_in(client, admin, session):
    from sqlalchemy import update

    from app.db.models import User

    await session.execute(update(User).where(User.id == admin.id).values(is_active=False))
    await session.commit()

    response = await login(client, "admin@hvops.local", "admin-password")
    assert response.status_code == 401


async def test_me_returns_the_current_user(client, viewer):
    response = await client.get("/auth/me", headers=auth_header(viewer))
    assert response.status_code == 200
    assert response.json()["email"] == "viewer@hvops.local"
    assert response.json()["role"] == "viewer"


async def test_me_requires_a_token(client):
    response = await client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_me_rejects_a_forged_token(client, viewer):
    import jwt

    forged = jwt.encode(
        {"sub": str(viewer.id), "type": "access"}, "wrong-secret", algorithm="HS256"
    )
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_refresh_rotates_the_token(client, admin):
    tokens = (await login(client, "admin@hvops.local", "admin-password")).json()

    response = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert rotated["access_token"]


async def test_reusing_a_rotated_refresh_token_is_rejected(client, admin):
    tokens = (await login(client, "admin@hvops.local", "admin-password")).json()
    first = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200

    replay = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert replay.status_code == 401
    assert "already used" in replay.json()["error"]["message"]


async def test_replay_revokes_the_whole_token_family(client, admin):
    """A replayed token means the family is compromised, so the fresh one dies too."""
    tokens = (await login(client, "admin@hvops.local", "admin-password")).json()
    rotated = (
        await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).json()

    await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    after = await client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})

    assert after.status_code == 401


async def test_expired_refresh_token_is_rejected(client, admin, session):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.db.models import RefreshToken

    tokens = (await login(client, "admin@hvops.local", "admin-password")).json()
    await session.execute(
        update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    response = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401
    assert "expired" in response.json()["error"]["message"]


async def test_logout_revokes_the_refresh_token(client, admin):
    tokens = (await login(client, "admin@hvops.local", "admin-password")).json()

    logout = await client.post(
        "/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert logout.status_code == 204

    response = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


async def test_refresh_without_any_token_is_rejected(client):
    assert (await client.post("/auth/refresh", json={})).status_code == 401


# --- role guards ----------------------------------------------------------


async def test_only_admins_create_users(client, viewer, operator, admin):
    payload = {"email": "new@hvops.local", "password": "password123", "role": "viewer"}

    assert (
        await client.post("/users", json=payload, headers=auth_header(viewer))
    ).status_code == 403
    assert (
        await client.post("/users", json=payload, headers=auth_header(operator))
    ).status_code == 403
    assert (
        await client.post("/users", json=payload, headers=auth_header(admin))
    ).status_code == 201


async def test_duplicate_email_conflicts(client, admin):
    payload = {"email": "dupe@hvops.local", "password": "password123", "role": "viewer"}
    assert (
        await client.post("/users", json=payload, headers=auth_header(admin))
    ).status_code == 201
    second = await client.post("/users", json=payload, headers=auth_header(admin))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


async def test_weak_password_is_rejected(client, admin):
    response = await client.post(
        "/users",
        json={"email": "weak@hvops.local", "password": "short", "role": "viewer"},
        headers=auth_header(admin),
    )
    assert response.status_code == 422


async def test_local_domain_emails_are_accepted(client, admin):
    """The spec's own seed account is admin@hvops.local - EmailStr would reject it."""
    response = await client.post(
        "/users",
        json={"email": "ops@hvops.local", "password": "password123", "role": "operator"},
        headers=auth_header(admin),
    )
    assert response.status_code == 201


async def test_login_writes_an_audit_row(client, admin, session):
    from sqlalchemy import select

    from app.db.models import AuditLog

    await login(client, "admin@hvops.local", "admin-password")

    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "auth.login" in actions
