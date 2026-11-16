"""Read-through cache for small protocol resources (compact index, legacy index, quick specs).

Each resource lives on disk as ``<hash>.body`` plus ``<hash>.json`` (validators and
timing). Freshness is decided by TTL; stale entries are revalidated with conditional
requests. When upstream is unreachable a previously cached body is served (stale-if-error).

The compact index ``/versions`` file is append-only upstream, so it is refreshed with a
ranged request and verified against upstream's ETag when that ETag is an MD5, falling back
to a full download whenever the check cannot be made.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from .config import Settings
from .upstream import Upstream, UpstreamError

log = logging.getLogger(__name__)

KEEP_HEADERS = ("etag", "last-modified", "content-type", "digest", "repr-digest")
LOCK_STRIPES = 256
ERROR_BACKOFF_SECONDS = 15
_MD5_HEX = re.compile(r"[0-9a-f]{32}")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)$")


@dataclass
class Entry:
    """A cache lookup result. ``status`` is 200 (``path`` set) or 404 (negative entry)."""

    status: int
    path: Optional[Path]
    meta: Dict[str, Any] = field(default_factory=dict)
    stale: bool = False


def replace_file(src: Path, dst: Path, attempts: int = 8) -> None:
    """``os.replace``, retried because Windows refuses to replace a file that is being read."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def md5_from_etag(etag: Optional[str]) -> Optional[str]:
    if not etag:
        return None
    value = etag.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"').lower()
    return value if _MD5_HEX.fullmatch(value) else None


def _md5_file(path: Path) -> str:
    # MD5 is what upstream publishes as the ETag of the index files; it is an integrity
    # check against truncation, not a security boundary.
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProxyCache:
    def __init__(self, settings: Settings, upstream: Upstream) -> None:
        self.root = settings.data_dir / "proxy"
        self.root.mkdir(parents=True, exist_ok=True)
        self.upstream = upstream
        self.negative_ttl = settings.negative_ttl
        self.stale_if_error = settings.stale_if_error
        self._locks = [threading.Lock() for _ in range(LOCK_STRIPES)]

    # -- paths and metadata -------------------------------------------------

    def _paths(self, namespace: str, key: str) -> Tuple[Path, Path, int]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        folder = self.root / namespace / digest[:2]
        return folder / f"{digest}.body", folder / f"{digest}.json", int(digest[:8], 16)

    @staticmethod
    def _load_meta(meta_path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_meta(meta_path: Path, meta: Dict[str, Any]) -> None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = meta_path.with_name(f"{meta_path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(meta), "utf-8")
        replace_file(tmp, meta_path)

    def _is_fresh(self, meta: Optional[Dict[str, Any]], body: Path, ttl: int) -> bool:
        if not meta:
            return False
        status = meta.get("status")
        if status == 200 and not body.exists():
            return False
        limit = self.negative_ttl if status == 404 else ttl
        return (time.time() - float(meta.get("fetched_at", 0))) < limit

    @staticmethod
    def _entry(meta: Dict[str, Any], body: Path, stale: bool = False) -> Entry:
        if meta.get("status") == 200:
            return Entry(200, body, meta, stale)
        return Entry(404, None, meta, stale)

    # -- public API ---------------------------------------------------------

    def get(
        self,
        namespace: str,
        key: str,
        upstream_path: str,
        ttl: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        appendable: bool = False,
    ) -> Entry:
        body, meta_path, stripe = self._paths(namespace, key)
        meta = self._load_meta(meta_path)
        if self._is_fresh(meta, body, ttl):
            return self._entry(meta or {}, body)
        with self._locks[stripe % LOCK_STRIPES]:
            meta = self._load_meta(meta_path)  # another thread or process may have refreshed it
            if self._is_fresh(meta, body, ttl):
                return self._entry(meta or {}, body)
            return self._refresh(upstream_path, params, body, meta_path, meta, ttl, appendable)

    # -- refresh ------------------------------------------------------------

    def _refresh(
        self,
        upstream_path: str,
        params: Optional[Dict[str, Any]],
        body: Path,
        meta_path: Path,
        meta: Optional[Dict[str, Any]],
        ttl: int,
        appendable: bool,
    ) -> Entry:
        have_body = bool(meta) and meta.get("status") == 200 and body.exists()  # type: ignore[union-attr]
        headers = {"Accept-Encoding": "identity"}
        ranged = False
        if have_body and meta:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            elif meta.get("last-modified"):
                headers["If-Modified-Since"] = meta["last-modified"]
            size = body.stat().st_size
            if appendable and size > 0 and meta.get("etag"):
                headers["Range"] = f"bytes={size - 1}-"
                ranged = True
        try:
            resp = self.upstream.get(upstream_path, params=params, headers=headers, stream=True)
        except UpstreamError as exc:
            return self._on_error(meta, body, meta_path, ttl, exc)
        try:
            status = resp.status_code
            if status == 304 and have_body and meta:
                meta["fetched_at"] = time.time()
                self._write_meta(meta_path, meta)
                return self._entry(meta, body)
            if status == 206 and ranged and meta:
                appended = self._try_append(resp, body, meta_path, meta)
                if appended is not None:
                    return appended
                resp.close()
                return self._full_fetch(upstream_path, params, body, meta_path, meta, ttl)
            if status == 416 and ranged and meta:
                resp.close()
                return self._full_fetch(upstream_path, params, body, meta_path, meta, ttl)
            if status == 200:
                return self._store_full(resp, body, meta_path)
            if status in (404, 410):
                return self._store_negative(body, meta_path)
            return self._on_error(
                meta, body, meta_path, ttl, UpstreamError(f"HTTP {status}", status)
            )
        finally:
            resp.close()

    def _full_fetch(
        self,
        upstream_path: str,
        params: Optional[Dict[str, Any]],
        body: Path,
        meta_path: Path,
        meta: Optional[Dict[str, Any]],
        ttl: int,
    ) -> Entry:
        try:
            resp = self.upstream.get(
                upstream_path, params=params, headers={"Accept-Encoding": "identity"}, stream=True
            )
        except UpstreamError as exc:
            return self._on_error(meta, body, meta_path, ttl, exc)
        try:
            if resp.status_code == 200:
                return self._store_full(resp, body, meta_path)
            if resp.status_code in (404, 410):
                return self._store_negative(body, meta_path)
            return self._on_error(
                meta,
                body,
                meta_path,
                ttl,
                UpstreamError(f"HTTP {resp.status_code}", resp.status_code),
            )
        finally:
            resp.close()

    @staticmethod
    def _meta_from_headers(resp: requests.Response, status: int) -> Dict[str, Any]:
        meta: Dict[str, Any] = {"status": status, "fetched_at": time.time()}
        for name in KEEP_HEADERS:
            value = resp.headers.get(name)
            if value:
                meta[name] = value
        return meta

    def _store_full(self, resp: requests.Response, body: Path, meta_path: Path) -> Entry:
        body.parent.mkdir(parents=True, exist_ok=True)
        tmp = body.with_name(f"{body.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    fh.write(chunk)
        except (requests.RequestException, OSError) as exc:
            tmp.unlink(missing_ok=True)
            raise UpstreamError(f"download interrupted: {exc}") from exc
        replace_file(tmp, body)
        meta = self._meta_from_headers(resp, 200)
        self._write_meta(meta_path, meta)
        return Entry(200, body, meta)

    def _store_negative(self, body: Path, meta_path: Path) -> Entry:
        body.unlink(missing_ok=True)
        meta = {"status": 404, "fetched_at": time.time()}
        self._write_meta(meta_path, meta)
        return Entry(404, None, meta)

    def _try_append(
        self, resp: requests.Response, body: Path, meta_path: Path, meta: Dict[str, Any]
    ) -> Optional[Entry]:
        """Apply a ranged tail to the local file; ``None`` means "do a full download"."""
        size = body.stat().st_size
        match = _CONTENT_RANGE.match(resp.headers.get("Content-Range", ""))
        if not match or int(match.group(1)) != size - 1:
            return None
        expected_md5 = md5_from_etag(resp.headers.get("ETag"))
        if expected_md5 is None:
            return None  # cannot verify the result, so do not trust an append
        try:
            tail = resp.content
        except requests.RequestException:
            return None
        if not tail.startswith(b"\n"):
            return None
        tmp = body.with_name(f"{body.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(body, tmp)
            with open(tmp, "ab") as fh:
                fh.write(tail[1:])
            if _md5_file(tmp) != expected_md5:
                tmp.unlink(missing_ok=True)
                log.info("Ranged update of %s failed verification; refetching in full", body.name)
                return None
            replace_file(tmp, body)
        except OSError:
            tmp.unlink(missing_ok=True)
            return None
        new_meta = self._meta_from_headers(resp, 200)
        self._write_meta(meta_path, new_meta)
        return Entry(200, body, new_meta)

    def _on_error(
        self,
        meta: Optional[Dict[str, Any]],
        body: Path,
        meta_path: Path,
        ttl: int,
        exc: UpstreamError,
    ) -> Entry:
        usable = bool(meta) and meta.get("status") == 200 and body.exists()  # type: ignore[union-attr]
        if usable and meta:
            age = time.time() - float(meta.get("fetched_at", 0))
            if age < self.stale_if_error:
                log.warning("Upstream failed (%s); serving stale copy of %s", exc, body.name)
                # Retry upstream only after a short back-off instead of on every request.
                meta["fetched_at"] = time.time() - max(ttl - ERROR_BACKOFF_SECONDS, 0)
                with contextlib.suppress(OSError):
                    self._write_meta(meta_path, meta)
                return Entry(200, body, meta, stale=True)
        raise exc
