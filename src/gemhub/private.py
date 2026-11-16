"""Private gems: pushed to this hub, served alongside (and ahead of) upstream gems."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

from .auth import has_scope
from .cache import Cache
from .config import Settings
from .extensions import db
from .gemfile import InvalidGem, ParsedGem, parse_gem
from .gems import bump_daily
from .models import ApiKey, Gem, PrivateGem, PrivateRelease, utcnow
from .names import split_candidates, valid_gem_filename, valid_gem_name
from .proxy import replace_file
from .rubymarshal import RDependency, RRequirement, SpecData, quick_spec
from .upstream import Upstream, UpstreamError

log = logging.getLogger(__name__)

TOKEN_KEY = "priv:token"


class PrivateError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _req_text(parts: List[List[str]]) -> str:
    return "&".join(f"{op} {version}" for op, version in parts)


def _is_default(parts: List[List[str]]) -> bool:
    return len(parts) == 1 and list(parts[0]) == [">=", "0"]


class PrivateService:
    def __init__(self, settings: Settings, upstream: Upstream, cache: Cache) -> None:
        self.settings = settings
        self.upstream = upstream
        self.cache = cache
        self.root = settings.data_dir / "private"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- state --------------------------------------------------------------

    def token(self) -> str:
        """Changes whenever private content changes; empty when there are no private gems."""
        cached = self.cache.get_json(TOKEN_KEY)
        if cached is not None:
            return cached["t"]
        gems = db.session.scalar(select(func.count()).select_from(PrivateGem)) or 0
        newest = db.session.scalar(select(func.coalesce(func.max(PrivateRelease.id), 0))) or 0
        yanked = (
            db.session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            case((PrivateRelease.yanked.is_(True), PrivateRelease.id), else_=0)
                        ),
                        0,
                    )
                )
            )
            or 0
        )
        token = "" if gems == 0 else f"{gems}.{newest}.{yanked}"
        self.cache.set_json(TOKEN_KEY, {"t": token}, 2)
        return token

    def invalidate(self, name: Optional[str] = None) -> None:
        self.cache.delete(TOKEN_KEY)
        if name:
            self.cache.delete(f"gem:{name}")
            self.cache.delete(f"gemmiss:{name}")

    def path_for(self, filename: str) -> Path:
        if not valid_gem_filename(filename):
            raise PrivateError(400, "invalid file name")
        return self.root / filename[0].lower() / filename

    def is_private(self, name: str) -> bool:
        if not valid_gem_name(name):
            return False
        return (
            db.session.execute(select(PrivateGem.id).where(PrivateGem.name == name)).first()
            is not None
        )

    def shadows_file(self, filename: str) -> bool:
        """True when ``filename`` belongs to a private gem name (so it must never fall through)."""
        stem = filename[: -len(".gem")] if filename.endswith(".gem") else filename
        stem = stem[: -len(".gemspec.rz")] if stem.endswith(".gemspec.rz") else stem
        return any(self.is_private(name) for name, _ in split_candidates(stem))

    # -- index generation ---------------------------------------------------

    def _live_releases(self, name: Optional[str] = None) -> List[PrivateRelease]:
        stmt = select(PrivateRelease).where(PrivateRelease.yanked.is_(False))
        if name is not None:
            stmt = stmt.where(PrivateRelease.gem_name == name)
        return list(db.session.execute(stmt.order_by(PrivateRelease.id.asc())).scalars())

    def live_names(self) -> List[str]:
        return sorted({r.gem_name for r in self._live_releases()})

    @staticmethod
    def _label(release: PrivateRelease) -> str:
        return (
            release.version
            if release.platform == "ruby"
            else f"{release.version}-{release.platform}"
        )

    def info_text(self, name: str) -> str:
        lines = ["---"]
        for release in self._live_releases(name):
            spec = release.spec
            deps = ",".join(
                f"{d['name']}:{_req_text(d['requirements'])}"
                for d in spec.get("dependencies", [])
                if d.get("kind") == "runtime"
            )
            extras = [f"checksum:{release.sha256}"]
            if not _is_default(spec.get("required_ruby", [[">=", "0"]])):
                extras.append(f"ruby:{_req_text(spec['required_ruby'])}")
            if not _is_default(spec.get("required_rubygems", [[">=", "0"]])):
                extras.append(f"rubygems:{_req_text(spec['required_rubygems'])}")
            lines.append(f"{self._label(release)} {deps}|{','.join(extras)}")
        return "\n".join(lines) + "\n"

    def versions_lines(self) -> bytes:
        import hashlib

        out = []
        for name in self.live_names():
            labels = [self._label(r) for r in self._live_releases(name)]
            info_md5 = hashlib.md5(self.info_text(name).encode(), usedforsecurity=False).hexdigest()
            out.append(f"{name} {','.join(labels)} {info_md5}")
        return ("\n".join(out) + "\n").encode() if out else b""

    # -- files and specs ----------------------------------------------------

    def release_for_filename(self, filename: str) -> Optional[PrivateRelease]:
        return db.session.execute(
            select(PrivateRelease).where(PrivateRelease.filename == filename)
        ).scalar_one_or_none()

    @staticmethod
    def spec_data(release: PrivateRelease) -> SpecData:
        spec: Dict[str, Any] = release.spec

        def req(parts: List[List[str]]) -> RRequirement:
            return RRequirement(tuple((op, v) for op, v in parts))

        email = spec.get("email")
        return SpecData(
            name=release.gem_name,
            version=release.version,
            summary=spec.get("summary") or "",
            description=spec.get("description"),
            authors=spec.get("authors") or [],
            email=email,
            homepage=spec.get("homepage"),
            licenses=spec.get("licenses") or [],
            metadata=spec.get("metadata") or {},
            required_ruby_version=req(spec.get("required_ruby", [[">=", "0"]])),
            required_rubygems_version=req(spec.get("required_rubygems", [[">=", "0"]])),
            dependencies=[
                RDependency(d["name"], req(d["requirements"]), d["kind"])
                for d in spec.get("dependencies", [])
            ],
            rubygems_version=spec.get("rubygems_version") or "3.4.20",
            date=release.pushed_at,
        )

    def quick_spec_for(self, filename_stem: str) -> Optional[bytes]:
        release = self.release_for_filename(filename_stem + ".gem")
        if release is None or release.yanked:
            return None
        return quick_spec(self.spec_data(release))

    def record_hit(self, release: PrivateRelease) -> None:
        try:
            db.session.execute(
                update(PrivateRelease)
                .where(PrivateRelease.id == release.id)
                .values(downloads=PrivateRelease.downloads + 1)
            )
            db.session.execute(
                update(Gem)
                .where(Gem.name == release.gem_name, Gem.is_private.is_(True))
                .values(downloads=Gem.downloads + 1)
            )
            bump_daily(release.gem_name, utcnow().date())
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("Could not record private download of %s", release.filename)

    # -- push, yank ---------------------------------------------------------

    def push(self, data: bytes, key: ApiKey) -> ParsedGem:
        if len(data) == 0:
            raise PrivateError(400, "Empty upload: send the .gem file as the request body.")
        if len(data) > self.settings.max_gem_bytes:
            raise PrivateError(413, "The gem is larger than this hub accepts.")
        try:
            parsed = parse_gem(data)
        except InvalidGem as exc:
            raise PrivateError(422, f"Rejected gem: {exc}") from exc
        if parsed.platform != "ruby":
            raise PrivateError(
                422, "Platform-specific gems are not supported for private gems yet."
            )

        gem = db.session.execute(
            select(PrivateGem).where(PrivateGem.name == parsed.name)
        ).scalar_one_or_none()
        if gem is None:
            self._check_public_name(parsed.name)
            gem = PrivateGem(name=parsed.name, owner_key_id=key.id)
            db.session.add(gem)
            try:
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                raise PrivateError(409, "Someone else just created this gem; try again.") from None
        elif gem.owner_key_id != key.id and not has_scope(key, "admin"):
            raise PrivateError(403, f"This API key does not own the gem {parsed.name}.")

        duplicate = db.session.execute(
            select(PrivateRelease.id).where(
                PrivateRelease.gem_id == gem.id,
                PrivateRelease.version == parsed.version,
                PrivateRelease.platform == parsed.platform,
            )
        ).first()
        if duplicate:
            db.session.rollback()
            raise PrivateError(
                409, f"Repushing of gem versions is not allowed: {parsed.name} {parsed.version}."
            )

        path = self.path_for(parsed.filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
        tmp.write_bytes(data)
        release = PrivateRelease(
            gem_id=gem.id,
            gem_name=parsed.name,
            version=parsed.version,
            platform=parsed.platform,
            filename=parsed.filename,
            sha256=parsed.sha256,
            size=parsed.size,
            spec=self._spec_json(parsed),
            pushed_by_key_id=key.id,
        )
        db.session.add(release)
        try:
            db.session.flush()
            replace_file(tmp, path)
            self._sync_gem_row(parsed.name)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            tmp.unlink(missing_ok=True)
            raise PrivateError(409, "This version was just pushed by someone else.") from None
        except Exception:
            db.session.rollback()
            tmp.unlink(missing_ok=True)
            raise
        self.invalidate(parsed.name)
        return parsed

    def _check_public_name(self, name: str) -> None:
        if self.settings.private_name_policy == "allow":
            return
        try:
            response = self.upstream.get(f"/info/{name}")
        except UpstreamError:
            raise PrivateError(
                503,
                "Cannot confirm the name is free on rubygems.org because upstream is "
                "unreachable. Retry, or set GEMHUB_PRIVATE_NAME_POLICY=allow.",
            ) from None
        try:
            status = response.status_code
        finally:
            response.close()
        if status == 200:
            raise PrivateError(
                409,
                f"A public gem named {name} exists on rubygems.org. Pushing it here would "
                "hijack that name for every client of this hub (dependency confusion). Rename "
                "the gem, or set GEMHUB_PRIVATE_NAME_POLICY=allow if that is intended.",
            )
        if status != 404:
            raise PrivateError(503, f"Upstream answered HTTP {status} while checking the name.")

    @staticmethod
    def _spec_json(parsed: ParsedGem) -> Dict[str, Any]:
        return {
            "summary": parsed.summary,
            "description": parsed.description,
            "authors": parsed.authors,
            "email": parsed.email,
            "homepage": parsed.homepage,
            "licenses": parsed.licenses,
            "metadata": parsed.metadata,
            "required_ruby": [list(p) for p in parsed.required_ruby],
            "required_rubygems": [list(p) for p in parsed.required_rubygems],
            "dependencies": [
                {
                    "name": d["name"],
                    "kind": d["kind"],
                    "requirements": [list(p) for p in d["requirements"]],
                }
                for d in parsed.dependencies
            ],
            "rubygems_version": parsed.rubygems_version,
            "date": parsed.date.isoformat(),
        }

    def set_yanked(self, name: str, version: str, platform: str, key: ApiKey, yanked: bool) -> None:
        gem = db.session.execute(
            select(PrivateGem).where(PrivateGem.name == name)
        ).scalar_one_or_none()
        if gem is None:
            raise PrivateError(404, f"{name} is not a private gem on this hub.")
        if gem.owner_key_id != key.id and not has_scope(key, "admin"):
            raise PrivateError(403, f"This API key does not own the gem {name}.")
        release = db.session.execute(
            select(PrivateRelease).where(
                PrivateRelease.gem_id == gem.id,
                PrivateRelease.version == version,
                PrivateRelease.platform == platform,
            )
        ).scalar_one_or_none()
        if release is None:
            raise PrivateError(404, f"Version {version} of {name} was not found.")
        if release.yanked == yanked:
            raise PrivateError(
                422, f"Version {version} is already {'yanked' if yanked else 'available'}."
            )
        release.yanked = yanked
        db.session.flush()
        self._sync_gem_row(name)
        db.session.commit()
        self.invalidate(name)

    # -- web-facing metadata ------------------------------------------------

    def _sync_gem_row(self, name: str) -> None:
        """Mirror a private gem into the ``gems`` table so the web hub treats it like any other."""
        releases = list(
            db.session.execute(
                select(PrivateRelease)
                .where(PrivateRelease.gem_name == name)
                .order_by(PrivateRelease.id.desc())
            ).scalars()
        )
        live = [r for r in releases if not r.yanked]
        latest = live[0] if live else None
        owner = db.session.execute(
            select(ApiKey.name)
            .join(PrivateGem, PrivateGem.owner_key_id == ApiKey.id)
            .where(PrivateGem.name == name)
        ).scalar_one_or_none()
        spec = latest.spec if latest else (releases[0].spec if releases else {})
        meta = spec.get("metadata", {})
        deps = {"runtime": [], "development": []}
        for dep in spec.get("dependencies", []):
            deps[dep["kind"]].append(
                {"name": dep["name"], "requirements": _req_text(dep["requirements"])}
            )
        data: Dict[str, Any] = {
            "name": name,
            "version": latest.version if latest else "",
            "downloads": sum(r.downloads for r in releases),
            "version_downloads": latest.downloads if latest else 0,
            "platform": "ruby",
            "authors": ", ".join(spec.get("authors", [])),
            "info": spec.get("description") or spec.get("summary") or "",
            "licenses": spec.get("licenses", []),
            "sha": latest.sha256 if latest else "",
            "version_created_at": latest.pushed_at.isoformat() if latest else None,
            "homepage_uri": spec.get("homepage") or meta.get("homepage_uri"),
            "source_code_uri": meta.get("source_code_uri"),
            "documentation_uri": meta.get("documentation_uri"),
            "changelog_uri": meta.get("changelog_uri"),
            "bug_tracker_uri": meta.get("bug_tracker_uri"),
            "dependencies": deps,
        }
        versions = [
            {
                "number": r.version,
                "platform": r.platform,
                "created_at": r.pushed_at.isoformat(),
                "downloads_count": r.downloads,
                "prerelease": any(c.isalpha() for c in r.version),
                "ruby_version": _req_text(r.spec.get("required_ruby", [])) or None,
                "sha": r.sha256,
                "licenses": r.spec.get("licenses", []),
            }
            for r in live
        ]
        row = db.session.execute(select(Gem).where(Gem.name == name)).scalar_one_or_none()
        now = utcnow()
        if row is None:
            row = Gem(name=name, first_cached_at=now)
            db.session.add(row)
        row.is_private = True
        row.latest_version = data["version"] or None
        row.summary = data["info"]
        row.downloads = data["downloads"]
        row.data = data
        row.versions = versions
        row.owners = [{"handle": owner}] if owner else []
        row.fetched_at = now

    def search(self, query: str, limit: int = 30) -> List[Dict[str, Any]]:
        needle = query.strip().lower()
        if not needle:
            return []
        rows = db.session.execute(
            select(Gem)
            .where(Gem.is_private.is_(True), func.lower(Gem.name).contains(needle, autoescape=True))
            .order_by(Gem.name)
            .limit(limit)
        ).scalars()
        return [
            {
                "name": g.name,
                "version": g.latest_version,
                "info": g.summary,
                "downloads": g.downloads,
                "authors": (g.data or {}).get("authors"),
                "private": True,
            }
            for g in rows
        ]


__all__ = ["PrivateError", "PrivateService", "timedelta", "valid_gem_filename"]
