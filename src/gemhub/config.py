"""Runtime configuration, read from ``GEMHUB_*`` environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from . import __version__

ENV_PREFIX = "GEMHUB_"


class ConfigError(ValueError):
    """Raised when an environment variable holds an invalid value."""


def _raw(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(ENV_PREFIX + name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{ENV_PREFIX}{name} must be >= {minimum}, got {value}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _raw(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{ENV_PREFIX}{name} must be a boolean, got {raw!r}")


def _policy(env: Mapping[str, str]) -> str:
    raw = (_raw(env, "PRIVATE_NAME_POLICY") or "reject-public").lower()
    if raw not in {"reject-public", "allow"}:
        raise ConfigError(f"{ENV_PREFIX}PRIVATE_NAME_POLICY must be 'reject-public' or 'allow'")
    return raw


def _normalize_database_url(url: str) -> str:
    # Some platforms still hand out the legacy "postgres://" scheme, which SQLAlchemy rejects.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    data_dir: Path
    database_url: str
    redis_url: Optional[str]
    upstream_url: str
    public_url: Optional[str]
    host: str
    port: int
    threads: int
    trust_proxy_hops: int
    auto_migrate: bool
    # Metadata freshness: Redis is deliberately shorter-lived than the database.
    redis_ttl: int
    db_ttl: int
    # Protocol caches (seconds before the hub revalidates against upstream).
    versions_ttl: int
    info_ttl: int
    specs_ttl: int
    quick_ttl: int
    negative_ttl: int
    stale_if_error: int
    # Gem file cache.
    cache_max_bytes: int
    verify_checksums: bool
    # Upstream HTTP client.
    connect_timeout: int
    read_timeout: int
    retries: int
    user_agent: str
    # Private gems.
    max_gem_bytes: int
    private_name_policy: str
    # Web UI.
    page_size: int
    log_level: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Settings:
        env = os.environ if env is None else env
        data_dir = Path(_raw(env, "DATA_DIR") or "./data").expanduser().resolve()
        database_url = _raw(env, "DATABASE_URL") or (
            "sqlite:///" + (data_dir / "gemhub.db").as_posix()
        )
        upstream = (_raw(env, "UPSTREAM_URL") or "https://rubygems.org").rstrip("/")
        if not upstream.startswith(("http://", "https://")):
            raise ConfigError(f"{ENV_PREFIX}UPSTREAM_URL must start with http:// or https://")
        public = _raw(env, "PUBLIC_URL")
        if public is not None:
            public = public.rstrip("/")
            if not public.startswith(("http://", "https://")):
                raise ConfigError(f"{ENV_PREFIX}PUBLIC_URL must start with http:// or https://")
        return cls(
            data_dir=data_dir,
            database_url=_normalize_database_url(database_url),
            redis_url=_raw(env, "REDIS_URL"),
            upstream_url=upstream,
            public_url=public,
            host=_raw(env, "HOST") or "127.0.0.1",
            port=_int(env, "PORT", 8080, 1),
            threads=_int(env, "THREADS", 16, 1),
            trust_proxy_hops=_int(env, "TRUST_PROXY_HOPS", 0),
            auto_migrate=_bool(env, "AUTO_MIGRATE", True),
            redis_ttl=_int(env, "REDIS_TTL", 3600, 1),
            db_ttl=_int(env, "DB_TTL", 6 * 3600, 1),
            versions_ttl=_int(env, "VERSIONS_TTL", 60),
            info_ttl=_int(env, "INFO_TTL", 15),
            specs_ttl=_int(env, "SPECS_TTL", 900),
            quick_ttl=_int(env, "QUICK_TTL", 86400),
            negative_ttl=_int(env, "NEGATIVE_TTL", 60),
            stale_if_error=_int(env, "STALE_IF_ERROR", 7 * 86400),
            cache_max_bytes=_int(env, "CACHE_MAX_BYTES", 0),
            verify_checksums=_bool(env, "VERIFY_CHECKSUMS", True),
            connect_timeout=_int(env, "CONNECT_TIMEOUT", 5, 1),
            read_timeout=_int(env, "READ_TIMEOUT", 30, 1),
            retries=_int(env, "UPSTREAM_RETRIES", 3),
            user_agent=_raw(env, "USER_AGENT")
            or f"gemhub/{__version__} (+https://github.com/cumulus13/gemhub)",
            max_gem_bytes=_int(env, "MAX_GEM_BYTES", 100 * 1024 * 1024, 1024),
            private_name_policy=_policy(env),
            page_size=_int(env, "PAGE_SIZE", 24, 1),
            log_level=(_raw(env, "LOG_LEVEL") or "INFO").upper(),
        )
