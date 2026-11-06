"""Source CRUD, credential encryption and the sync trigger."""

from __future__ import annotations

import httpx
import respx
from sqlalchemy import select

from app.db.models import AuditLog, HypervisorSource
from tests.conftest import auth_header

PAYLOAD = {
    "name": "nutanix-prod",
    "type": "nutanix",
    "endpoint": "https://prism.example.com:9440",
    "username": "svc-hvops",
    "password": "super-secret",
    "verify_ssl": True,
}


async def test_viewer_can_list_but_not_create(client, viewer, source):
    assert (await client.get("/sources", headers=auth_header(viewer))).status_code == 200
    assert (
        await client.post("/sources", json=PAYLOAD, headers=auth_header(viewer))
    ).status_code == 403


async def test_operator_cannot_create_a_source(client, operator):
    assert (
        await client.post("/sources", json=PAYLOAD, headers=auth_header(operator))
    ).status_code == 403


async def test_admin_creates_a_source_without_echoing_the_password(client, admin):
    response = await client.post("/sources", json=PAYLOAD, headers=auth_header(admin))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "nutanix-prod"
    assert "password" not in body
    assert "password_encrypted" not in body


async def test_the_password_is_encrypted_at_rest(client, admin, session):
    await client.post("/sources", json=PAYLOAD, headers=auth_header(admin))

    row = (
        await session.execute(
            select(HypervisorSource).where(HypervisorSource.name == "nutanix-prod")
        )
    ).scalar_one()

    assert row.password_encrypted != "super-secret"
    assert "super-secret" not in row.password_encrypted

    from app.security import decrypt_secret

    assert decrypt_secret(row.password_encrypted) == "super-secret"


async def test_duplicate_source_name_conflicts(client, admin):
    await client.post("/sources", json=PAYLOAD, headers=auth_header(admin))
    second = await client.post("/sources", json=PAYLOAD, headers=auth_header(admin))
    assert second.status_code == 409


async def test_patch_updates_fields_and_rotates_the_password(client, admin, source, session):
    response = await client.patch(
        f"/sources/{source.id}",
        json={"is_enabled": False, "password": "rotated"},
        headers=auth_header(admin),
    )
    assert response.status_code == 200
    assert response.json()["is_enabled"] is False

    row = (
        await session.execute(select(HypervisorSource).where(HypervisorSource.id == source.id))
    ).scalar_one()
    await session.refresh(row)

    from app.security import decrypt_secret

    assert decrypt_secret(row.password_encrypted) == "rotated"


async def test_patch_never_writes_the_password_to_the_audit_log(client, admin, source, session):
    await client.patch(
        f"/sources/{source.id}", json={"password": "do-not-log-me"}, headers=auth_header(admin)
    )

    rows = (
        (await session.execute(select(AuditLog).where(AuditLog.action == "source.update")))
        .scalars()
        .all()
    )
    assert rows
    assert all("do-not-log-me" not in str(row.details) for row in rows)
    assert all("password" not in row.details for row in rows)


async def test_patching_an_unknown_source_is_404(client, admin):
    response = await client.patch(
        "/sources/11111111-1111-1111-1111-111111111111",
        json={"is_enabled": False},
        headers=auth_header(admin),
    )
    assert response.status_code == 404


@respx.mock
async def test_operator_can_trigger_a_sync(client, operator, source):
    route = respx.post("http://collector:5000/collect/trigger").mock(
        return_value=httpx.Response(200, json={"detail": "collected 1 source(s), 0 failed"})
    )

    response = await client.post(f"/sources/{source.id}/sync", headers=auth_header(operator))

    assert response.status_code == 200
    assert response.json() == {"triggered": True, "detail": "collected 1 source(s), 0 failed"}
    assert route.called
    # The collector is reached with the service token, not a user JWT.
    assert route.calls[0].request.headers["X-Service-Token"]


@respx.mock
async def test_viewer_cannot_trigger_a_sync(client, viewer, source):
    respx.post("http://collector:5000/collect/trigger").mock(
        return_value=httpx.Response(200, json={"detail": "ok"})
    )
    response = await client.post(f"/sources/{source.id}/sync", headers=auth_header(viewer))
    assert response.status_code == 403


@respx.mock
async def test_an_unreachable_collector_is_a_503_not_a_500(client, operator, source):
    respx.post("http://collector:5000/collect/trigger").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    response = await client.post(f"/sources/{source.id}/sync", headers=auth_header(operator))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


async def test_every_mutation_writes_an_audit_row(client, admin, source, session):
    await client.post("/sources", json=PAYLOAD, headers=auth_header(admin))
    await client.patch(
        f"/sources/{source.id}", json={"is_enabled": True}, headers=auth_header(admin)
    )

    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "source.create" in actions
    assert "source.update" in actions
