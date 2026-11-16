"""Database models. Portable across SQLite, PostgreSQL and MySQL/MariaDB."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import false as sa_false

from .extensions import db


def utcnow() -> datetime:
    """Naive UTC timestamp (stored without tzinfo so every dialect behaves the same)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Gem(db.Model):  # type: ignore[name-defined]
    """Metadata of one gem as last fetched from upstream."""

    __tablename__ = "gems"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    latest_version = db.Column(db.String(128))
    summary = db.Column(db.Text)
    downloads = db.Column(db.BigInteger, nullable=False, default=0, index=True)
    is_private = db.Column(db.Boolean, nullable=False, default=False, server_default=sa_false())
    data = db.Column(db.JSON, nullable=False)
    versions = db.Column(db.JSON, nullable=False)
    owners = db.Column(db.JSON, nullable=False)
    first_cached_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    fetched_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CachedFile(db.Model):  # type: ignore[name-defined]
    """A ``.gem`` file stored on disk, with hit counters used for stats and eviction."""

    __tablename__ = "cached_files"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False, unique=True)
    gem_name = db.Column(db.String(255), nullable=False, index=True)
    size = db.Column(db.BigInteger, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False, default="")
    verified = db.Column(db.Boolean, nullable=False, default=False)
    fetched_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_access_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    hits = db.Column(db.Integer, nullable=False, default=0)


class DailyDownload(db.Model):  # type: ignore[name-defined]
    """Gem files served by this hub per gem per UTC day."""

    __tablename__ = "daily_downloads"

    gem_name = db.Column(db.String(255), primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    count = db.Column(db.Integer, nullable=False, default=0)


class ApiKey(db.Model):  # type: ignore[name-defined]
    """An API key allowed to push or yank private gems. Only a SHA-256 hash is stored."""

    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    key_hash = db.Column(db.String(64), nullable=False, unique=True)
    scopes = db.Column(db.String(100), nullable=False, default="push,yank")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)


class PrivateGem(db.Model):  # type: ignore[name-defined]
    """A gem name owned by this hub. Its name shadows any upstream gem of the same name."""

    __tablename__ = "private_gems"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    owner_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class PrivateRelease(db.Model):  # type: ignore[name-defined]
    """One pushed version of a private gem."""

    __tablename__ = "private_releases"
    __table_args__ = (db.UniqueConstraint("gem_id", "version", "platform", name="uq_release"),)

    id = db.Column(db.Integer, primary_key=True)
    gem_id = db.Column(
        db.Integer, db.ForeignKey("private_gems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gem_name = db.Column(db.String(255), nullable=False, index=True)
    version = db.Column(db.String(128), nullable=False)
    platform = db.Column(db.String(64), nullable=False, default="ruby")
    filename = db.Column(db.String(300), nullable=False, unique=True)
    sha256 = db.Column(db.String(64), nullable=False)
    size = db.Column(db.BigInteger, nullable=False)
    spec = db.Column(db.JSON, nullable=False)
    yanked = db.Column(db.Boolean, nullable=False, default=False)
    downloads = db.Column(db.Integer, nullable=False, default=0)
    pushed_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    pushed_by_key_id = db.Column(db.Integer, db.ForeignKey("api_keys.id", ondelete="SET NULL"))
