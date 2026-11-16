"""Gem metadata for the web UI: Redis (short TTL) in front of the database (long TTL)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List
from urllib.parse import quote

from sqlalchemy import select

from .cache import Cache
from .config import Settings
from .extensions import db
from .models import Gem, utcnow
from .names import valid_gem_name
from .upstream import Upstream, UpstreamError, UpstreamNotFound

log = logging.getLogger(__name__)

LOCK_STRIPES = 128
VERSION_FIELDS = (
    "number",
    "platform",
    "created_at",
    "downloads_count",
    "prerelease",
    "ruby_version",
    "sha",
    "licenses",
)
SEARCH_FIELDS = ("name", "version", "info", "downloads", "version_downloads", "authors")


class GemNotFound(Exception):
    pass


class UpstreamUnavailable(Exception):
    pass


@dataclass(frozen=True)
class GemView:
    name: str
    data: Dict[str, Any]
    versions: List[Dict[str, Any]]
    owners: List[Dict[str, Any]]
    fetched_at: datetime
    origin: str  # "cache" (Redis or memory), "database", "upstream"
    stale: bool = False
    private: bool = False


def _trim_versions(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [{k: v.get(k) for k in VERSION_FIELDS if k in v} for v in raw if isinstance(v, dict)]


class MetadataService:
    def __init__(self, settings: Settings, upstream: Upstream, cache: Cache) -> None:
        self.settings = settings
        self.upstream = upstream
        self.cache = cache
        self._locks = [threading.Lock() for _ in range(LOCK_STRIPES)]

    # -- gem detail ---------------------------------------------------------

    def get_gem(self, name: str, *, force_refresh: bool = False) -> GemView:
        if not valid_gem_name(name):
            raise GemNotFound(name)
        key = f"gem:{name}"
        local = self._row(name)
        if local is not None and local.is_private:
            return self._from_row(local, "private")  # never refreshed from upstream
        if not force_refresh:
            payload = self.cache.get_json(key)
            if payload:
                return self._from_payload(name, payload, "cache")
            if self.cache.get_json(f"gemmiss:{name}"):
                raise GemNotFound(name)
            row = self._row(name)
            if row is not None and self._is_db_fresh(row):
                view = self._from_row(row, "database")
                self.cache.set_json(key, self._payload(view), self.settings.redis_ttl)
                return view
        with self._locks[hash(name) % LOCK_STRIPES]:
            if not force_refresh:
                payload = self.cache.get_json(key)
                if payload:
                    return self._from_payload(name, payload, "cache")
                row = self._row(name)
                if row is not None and self._is_db_fresh(row):
                    view = self._from_row(row, "database")
                    self.cache.set_json(key, self._payload(view), self.settings.redis_ttl)
                    return view
            else:
                row = self._row(name)
            return self._refresh(name, row)

    def _row(self, name: str):
        row = db.session.execute(select(Gem).where(Gem.name == name)).scalar_one_or_none()
        # Some databases compare text case-insensitively; upstream names are case-sensitive.
        return row if row is not None and row.name == name else None

    def _is_db_fresh(self, row: Gem) -> bool:
        return utcnow() - row.fetched_at < timedelta(seconds=self.settings.db_ttl)

    def _refresh(self, name: str, row) -> GemView:
        quoted = quote(name, safe="")
        try:
            data = self.upstream.get_json(f"/api/v1/gems/{quoted}.json")
            versions = _trim_versions(self.upstream.get_json(f"/api/v1/versions/{quoted}.json"))
            try:
                owners_raw = self.upstream.get_json(f"/api/v1/gems/{quoted}/owners.json")
                owners = (
                    [o for o in owners_raw if isinstance(o, dict)]
                    if isinstance(owners_raw, list)
                    else []
                )
            except UpstreamNotFound:
                owners = []
            except UpstreamError as exc:
                log.info("Owners of %s unavailable: %s", name, exc)
                owners = list(row.owners) if row is not None else []
        except UpstreamNotFound:
            if row is not None:
                db.session.delete(row)
                db.session.commit()
            self.cache.delete(f"gem:{name}")
            self.cache.set_json(f"gemmiss:{name}", 1, self.settings.negative_ttl)
            raise GemNotFound(name) from None
        except UpstreamError as exc:
            if row is not None:
                log.warning("Upstream failed (%s); serving stored metadata for %s", exc, name)
                view = self._from_row(row, "database", stale=True)
                # Short TTL so a flapping upstream is not hammered on every page view.
                self.cache.set_json(f"gem:{name}", self._payload(view), 30)
                return view
            raise UpstreamUnavailable(str(exc)) from exc
        if not isinstance(data, dict):
            raise UpstreamUnavailable("unexpected response shape from upstream")

        now = utcnow()
        if row is None:
            row = Gem(name=name, first_cached_at=now)
            db.session.add(row)
        row.latest_version = str(data.get("version") or "")[:128] or None
        row.summary = data.get("info")
        row.downloads = int(data.get("downloads") or 0)
        row.data = data
        row.versions = versions
        row.owners = owners
        row.fetched_at = now
        db.session.commit()
        view = self._from_row(row, "upstream")
        self.cache.set_json(f"gem:{name}", self._payload(view), self.settings.redis_ttl)
        return view

    @staticmethod
    def _from_row(row: Gem, origin: str, stale: bool = False) -> GemView:
        return GemView(
            name=row.name,
            data=dict(row.data or {}),
            versions=list(row.versions or []),
            owners=list(row.owners or []),
            fetched_at=row.fetched_at,
            origin=origin,
            stale=stale,
            private=bool(row.is_private),
        )

    @staticmethod
    def _payload(view: GemView) -> Dict[str, Any]:
        return {
            "data": view.data,
            "versions": view.versions,
            "owners": view.owners,
            "fetched_at": view.fetched_at.isoformat(),
            "stale": view.stale,
        }

    @staticmethod
    def _from_payload(name: str, payload: Dict[str, Any], origin: str) -> GemView:
        return GemView(
            name=name,
            data=payload.get("data") or {},
            versions=payload.get("versions") or [],
            owners=payload.get("owners") or [],
            fetched_at=datetime.fromisoformat(payload["fetched_at"]),
            origin=origin,
            stale=bool(payload.get("stale")),
        )

    # -- search -------------------------------------------------------------

    def search(self, query: str, page: int = 1) -> List[Dict[str, Any]]:
        query = query.strip()[:100]
        if not query:
            return []
        key = f"search:{page}:{query.lower()}"
        cached = self.cache.get_json(key)
        if cached is not None:
            return cached
        try:
            raw = self.upstream.get_json(
                "/api/v1/search.json", params={"query": query, "page": page}
            )
        except UpstreamNotFound:
            raw = []
        except UpstreamError as exc:
            raise UpstreamUnavailable(str(exc)) from exc
        results = [
            {k: item.get(k) for k in SEARCH_FIELDS if k in item}
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict) and item.get("name")
        ]
        self.cache.set_json(key, results, self.settings.redis_ttl)
        return results

    # -- maintenance --------------------------------------------------------

    def forget(self, name: str) -> None:
        self.cache.delete(f"gem:{name}")
        self.cache.delete(f"gemmiss:{name}")
