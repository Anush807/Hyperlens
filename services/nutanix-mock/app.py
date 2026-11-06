"""Nutanix Prism Central v3 mock.

Serves the three list endpoints the collector uses, in the real Prism v3
envelope (api_version / metadata / entities), with basic auth, `offset` and
`length` paging. Nutanix CE needs hardware this project does not assume, but the
adapter code under test is the real one (spec 2, 7.4).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

FIXTURES = Path(__file__).parent / "fixtures"
API_VERSION = "3.1"

USERNAME = os.getenv("NUTANIX_USER", "admin")
PASSWORD = os.getenv("NUTANIX_PASSWORD", "admin")

app = FastAPI(title="Nutanix Prism v3 mock", version="1.0.0", docs_url="/docs")
security = HTTPBasic()

_cache: dict[str, list[dict[str, Any]]] = {}


def _entities(kind: str) -> list[dict[str, Any]]:
    if kind not in _cache:
        path = FIXTURES / f"{kind}.json"
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no fixture for kind {kind!r}")
        _cache[kind] = json.loads(path.read_text())
    return _cache[kind]


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(credentials.username, USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _page(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    entities = _entities(kind)
    offset = max(0, int(body.get("offset") or 0))
    length = max(1, min(int(body.get("length") or 20), 500))
    window = entities[offset : offset + length]
    return {
        "api_version": API_VERSION,
        "metadata": {
            "kind": kind,
            "total_matches": len(entities),
            "length": len(window),
            "offset": offset,
        },
        "entities": window,
    }


@app.get("/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {"status": "ok", "kinds": sorted(p.stem for p in FIXTURES.glob("*.json"))}


@app.post("/api/nutanix/v3/clusters/list")
def list_clusters(body: dict[str, Any] = Body(default={}), _: str = Depends(require_auth)):
    return _page("clusters", body)


@app.post("/api/nutanix/v3/hosts/list")
def list_hosts(body: dict[str, Any] = Body(default={}), _: str = Depends(require_auth)):
    return _page("hosts", body)


@app.post("/api/nutanix/v3/vms/list")
def list_vms(body: dict[str, Any] = Body(default={}), _: str = Depends(require_auth)):
    return _page("vms", body)


@app.post("/api/nutanix/v3/storage_containers/list")
def list_storage_containers(body: dict[str, Any] = Body(default={}), _: str = Depends(require_auth)):
    return _page("storage_containers", body)
