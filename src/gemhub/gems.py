"""On-disk store for ``.gem`` files: fetch once from upstream, verify, serve forever."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .cache import Cache
from .config import Settings
from .extensions import db
from .models import CachedFile, DailyDownload, utcnow
from .names import split_candidates, valid_gem_filename
from .proxy import ProxyCache, replace_file
from .upstream import Upstream, UpstreamError

log = logging.getLogger(__name__)

_CHECKSUM = re.compile(r"\|checksum:([0-9a-f]{64})")
LOCK_STRIPES = 128


def bump_daily(gem_name: str, day) -> None:
    result = db.session.execute(
        update(DailyDownload)
        .where(DailyDownload.gem_name == gem_name, DailyDownload.day == day)
        .values(count=DailyDownload.count + 1)
    )
    if result.rowcount == 0:
        try:
            with db.session.begin_nested():
                db.session.add(DailyDownload(gem_name=gem_name, day=day, count=1))
        except IntegrityError:  # lost a race with another worker; increment instead
            db.session.execute(
                update(DailyDownload)
                .where(DailyDownload.gem_name == gem_name, DailyDownload.day == day)
                .values(count=DailyDownload.count + 1)
            )


class InvalidFilename(Exception):
    pass


class GemFileNotFound(Exception):
    pass


class ChecksumMismatch(Exception):
    def __init__(self, filename: str, expected: str, actual: str) -> None:
        super().__init__(f"checksum mismatch for {filename}: expected {expected}, got {actual}")
        self.filename = filename
        self.expected = expected
        self.actual = actual


class GemStore:
    def __init__(
        self,
        settings: Settings,
        upstream: Upstream,
        proxy: ProxyCache,
        cache: Cache,
    ) -> None:
        self.settings = settings
        self.upstream = upstream
        self.proxy = proxy
        self.cache = cache
        self.root = settings.data_dir / "gems"
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = [threading.Lock() for _ in range(LOCK_STRIPES)]

    # -- paths --------------------------------------------------------------

    def path_for(self, filename: str) -> Path:
        if not valid_gem_filename(filename):
            raise InvalidFilename(filename)
        return self.root / filename[0].lower() / filename

    # -- fetching -----------------------------------------------------------

    def get_or_fetch(self, filename: str) -> Path:
        path = self.path_for(filename)
        if path.is_file():
            return path
        if self.cache.get_json(f"gemmiss:{filename}"):
            raise GemFileNotFound(filename)
        lock = self._locks[hash(filename) % LOCK_STRIPES]
        with lock:
            if path.is_file():
                return path
            return self._download(filename, path)

    def _download(self, filename: str, path: Path) -> Path:
        resp = self.upstream.get(f"/gems/{filename}", stream=True)
        try:
            if resp.status_code in (404, 410):
                self.cache.set_json(f"gemmiss:{filename}", 1, self.settings.negative_ttl)
                raise GemFileNotFound(filename)
            if resp.status_code != 200:
                raise UpstreamError(
                    f"gem download answered HTTP {resp.status_code}", resp.status_code
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
            sha = hashlib.sha256()
            size = 0
            try:
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(1 << 16):
                        fh.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
            except (requests.RequestException, OSError) as exc:
                tmp.unlink(missing_ok=True)
                raise UpstreamError(f"gem download interrupted: {exc}") from exc
        finally:
            resp.close()

        actual = sha.hexdigest()
        gem_name, expected = self._expected_checksum(filename)
        verified = False
        if self.settings.verify_checksums and expected is not None:
            if expected != actual:
                tmp.unlink(missing_ok=True)
                raise ChecksumMismatch(filename, expected, actual)
            verified = True
        elif self.settings.verify_checksums:
            log.warning("No upstream checksum available for %s; stored unverified", filename)
        if path.exists():  # another worker finished first; never replace a file in use
            tmp.unlink(missing_ok=True)
            return path
        replace_file(tmp, path)
        self._register(filename, gem_name or self._guess_name(filename), size, actual, verified)
        self._evict_if_needed(keep=filename)
        return path

    # -- checksums ----------------------------------------------------------

    @staticmethod
    def _guess_name(filename: str) -> str:
        candidates = split_candidates(filename[: -len(".gem")])
        return candidates[0][0] if candidates else filename[: -len(".gem")]

    def _expected_checksum(self, filename: str) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(gem_name, sha256)`` from the compact index ``info`` file, when available."""
        stem = filename[: -len(".gem")]
        for name, label in split_candidates(stem):
            try:
                entry = self.proxy.get("info", name, f"/info/{name}", self.settings.info_ttl)
            except UpstreamError:
                return None, None
            if entry.status != 200 or entry.path is None:
                continue
            try:
                lines: List[str] = entry.path.read_text("utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            prefix = label + " "
            for line in lines:
                if line.startswith(prefix):
                    match = _CHECKSUM.search(line)
                    return name, match.group(1) if match else None
        return None, None

    # -- bookkeeping --------------------------------------------------------

    def _register(
        self, filename: str, gem_name: str, size: int, sha256: str, verified: bool
    ) -> None:
        try:
            row = db.session.execute(
                select(CachedFile).where(CachedFile.filename == filename)
            ).scalar_one_or_none()
            now = utcnow()
            if row is None:
                db.session.add(
                    CachedFile(
                        filename=filename,
                        gem_name=gem_name,
                        size=size,
                        sha256=sha256,
                        verified=verified,
                        fetched_at=now,
                        last_access_at=now,
                        hits=0,
                    )
                )
            else:
                row.size, row.sha256, row.verified, row.fetched_at = size, sha256, verified, now
            db.session.commit()
        except Exception:  # bookkeeping must never fail a download
            db.session.rollback()
            log.exception("Could not record cached file %s", filename)

    def record_hit(self, filename: str, path: Path) -> None:
        """Count one served download (file totals and per-day totals)."""
        try:
            now = utcnow()
            result = db.session.execute(
                update(CachedFile)
                .where(CachedFile.filename == filename)
                .values(hits=CachedFile.hits + 1, last_access_at=now)
            )
            if result.rowcount == 0:
                db.session.add(
                    CachedFile(
                        filename=filename,
                        gem_name=self._guess_name(filename),
                        size=path.stat().st_size,
                        sha256="",
                        verified=False,
                        fetched_at=now,
                        last_access_at=now,
                        hits=1,
                    )
                )
            row = db.session.execute(
                select(CachedFile.gem_name).where(CachedFile.filename == filename)
            ).scalar_one_or_none()
            bump_daily(row or self._guess_name(filename), now.date())
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Could not record download of %s", filename)

    # -- eviction and maintenance ------------------------------------------

    def total_bytes(self) -> int:
        return int(db.session.scalar(select(func.coalesce(func.sum(CachedFile.size), 0))) or 0)

    def _evict_if_needed(self, keep: str) -> None:
        limit = self.settings.cache_max_bytes
        if limit <= 0:
            return
        try:
            total = self.total_bytes()
            if total <= limit:
                return
            target = int(limit * 0.9)
            rows = db.session.execute(
                select(CachedFile).order_by(CachedFile.last_access_at.asc())
            ).scalars()
            for row in rows:
                if total <= target:
                    break
                if row.filename == keep:
                    continue
                if self._remove_file(row.filename):
                    total -= row.size
                    db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Cache eviction failed")

    def _remove_file(self, filename: str) -> bool:
        try:
            self.path_for(filename).unlink(missing_ok=True)
            return True
        except (OSError, InvalidFilename):
            log.warning("Could not remove %s (in use?)", filename)
            return False

    def purge(self, older_than_days: int, dry_run: bool = False) -> Tuple[int, int]:
        """Remove files not served for ``older_than_days``. Returns ``(files, bytes)``."""
        cutoff = utcnow() - timedelta(days=older_than_days)
        rows = db.session.execute(select(CachedFile)).scalars().all()
        count = freed = 0
        for row in rows:
            if row.last_access_at >= cutoff:
                continue
            count += 1
            freed += row.size
            if not dry_run and self._remove_file(row.filename):
                db.session.delete(row)
        if not dry_run:
            db.session.commit()
        return count, freed
