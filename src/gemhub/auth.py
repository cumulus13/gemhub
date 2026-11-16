"""API keys for pushing and yanking private gems."""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Iterable, Optional, Tuple

from flask import abort, request
from sqlalchemy import select

from .extensions import db
from .models import ApiKey, utcnow

log = logging.getLogger(__name__)

VALID_SCOPES = ("push", "yank", "admin")
KEY_PREFIX = "gh_"


def hash_key(plain: str) -> str:
    # API keys are 256-bit random values, so a plain SHA-256 is sufficient (no password KDF).
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def parse_scopes(raw: Iterable[str]) -> str:
    scopes = []
    for scope in raw:
        scope = scope.strip().lower()
        if scope not in VALID_SCOPES:
            raise ValueError(f"unknown scope {scope!r}; choose from {', '.join(VALID_SCOPES)}")
        if scope not in scopes:
            scopes.append(scope)
    if not scopes:
        raise ValueError("at least one scope is required")
    return ",".join(scopes)


def create_key(name: str, scopes: str = "push,yank") -> Tuple[ApiKey, str]:
    """Create a key and return it with the plaintext, which is shown once and never stored."""
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("key name must be 1 to 100 characters")
    if db.session.execute(select(ApiKey).where(ApiKey.name == name)).scalar_one_or_none():
        raise ValueError(f"a key named {name!r} already exists")
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(name=name, key_hash=hash_key(plain), scopes=parse_scopes(scopes.split(",")))
    db.session.add(key)
    db.session.commit()
    return key, plain


def revoke_key(name: str) -> bool:
    key = db.session.execute(select(ApiKey).where(ApiKey.name == name)).scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        return False
    key.revoked_at = utcnow()
    db.session.commit()
    return True


def has_scope(key: ApiKey, scope: str) -> bool:
    scopes = key.scopes.split(",")
    return scope in scopes or "admin" in scopes


def authenticate(scope: str) -> Optional[ApiKey]:
    raw = request.headers.get("Authorization", "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return None
    key = db.session.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_key(raw))
    ).scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        return None
    key.last_used_at = utcnow()
    try:
        db.session.commit()
    except Exception:  # last-used bookkeeping must not block a push
        db.session.rollback()
    return key


def require_key(scope: str) -> ApiKey:
    key = authenticate(scope)
    if key is None:
        abort(401, "Missing or invalid API key. Send it in the Authorization header.")
    if not has_scope(key, scope):
        abort(403, f"This API key does not have the '{scope}' scope.")
    return key
