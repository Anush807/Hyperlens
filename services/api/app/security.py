"""Password hashing, JWT issue/verify, refresh-token hashing and credential encryption.

No FastAPI imports here on purpose: the assistant tools and CLI jobs reuse this.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def create_access_token(user_id: uuid.UUID, role: str, email: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_access_ttl_min)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError on anything wrong - callers translate to 401."""
    payload: dict[str, Any] = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return payload


def new_refresh_token() -> tuple[str, str, datetime]:
    """Returns (plaintext, sha256 hash, expiry). Only the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    expires = datetime.now(UTC) + timedelta(days=settings.jwt_refresh_ttl_days)
    return raw, hash_refresh_token(raw), expires


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _fernet() -> Fernet:
    return Fernet(settings.fernet_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:  # rotated FERNET_KEY, or hand-edited row
        raise ValueError(
            "stored credential cannot be decrypted with the current FERNET_KEY"
        ) from exc


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())
