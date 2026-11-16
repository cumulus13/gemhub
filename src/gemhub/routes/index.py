"""RubyGems protocol endpoints, served read-through from the upstream mirror.

Covers what ``gem`` and ``bundler`` use: the compact index (``/versions``, ``/names``,
``/info/<gem>``), the legacy index (``specs.4.8.gz`` and friends, ``/quick``), the
dependency API and the ``.gem`` files themselves.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from email.utils import parsedate_to_datetime
from typing import Optional

from flask import Blueprint, Response, abort, request, send_file

from ..gems import ChecksumMismatch, GemFileNotFound, InvalidFilename
from ..hub import get_hub
from ..names import valid_gem_name, valid_quick_filename
from ..proxy import Entry
from ..upstream import UpstreamError

log = logging.getLogger(__name__)
bp = Blueprint("index", __name__)

TEXT = "text/plain; charset=utf-8"
BINARY = "application/octet-stream"


def serve_entry(entry: Entry, default_mimetype: str) -> Response:
    """Send a cached resource with the validators and digests upstream gave us.

    ``conditional=True`` makes werkzeug answer ``If-None-Match``/``If-Modified-Since`` with
    304 and honour ``Range``, which bundler uses to download only the tail of ``/versions``.
    """
    if entry.status != 200 or entry.path is None:
        abort(404)
    meta = entry.meta
    etag = meta.get("etag")
    last_modified = None
    if meta.get("last-modified"):
        try:
            last_modified = parsedate_to_datetime(meta["last-modified"])
        except (TypeError, ValueError):
            last_modified = None
    response = send_file(
        entry.path,
        mimetype=meta.get("content-type") or default_mimetype,
        conditional=True,
        etag=etag.strip().removeprefix("W/").strip('"') if etag else True,
        last_modified=last_modified,
        max_age=0,
    )
    if etag:
        response.headers["ETag"] = etag
    for header in ("Digest", "Repr-Digest"):
        value = meta.get(header.lower())
        if value:
            response.headers[header] = value
    if entry.stale:
        response.headers["Warning"] = '110 gemhub "Response is stale"'
    return response


def cached(
    namespace: str,
    key: str,
    upstream_path: str,
    ttl: int,
    mimetype: str,
    *,
    params: Optional[dict] = None,
    appendable: bool = False,
) -> Response:
    hub = get_hub()
    try:
        entry = hub.proxy.get(
            namespace, key, upstream_path, ttl, params=params, appendable=appendable
        )
    except UpstreamError as exc:
        log.warning("Upstream unavailable for %s: %s", upstream_path, exc)
        abort(502, "Upstream RubyGems server is unavailable and nothing is cached yet.")
    return serve_entry(entry, mimetype)


# -- compact index -----------------------------------------------------------


def _bytes_response(data: bytes, mimetype: str = TEXT) -> Response:
    """Serve generated content with the same validators upstream's index files carry."""
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    response = send_file(io.BytesIO(data), mimetype=mimetype, conditional=True, etag=md5, max_age=0)
    sha = base64.b64encode(hashlib.sha256(data).digest()).decode()
    response.headers["Repr-Digest"] = f"sha-256=:{sha}:"
    return response


def _upstream_entry(namespace: str, key: str, path: str, ttl: int, appendable: bool = False):
    """Cached upstream resource, or ``None`` when upstream is down and nothing is cached."""
    try:
        entry = get_hub().proxy.get(namespace, key, path, ttl, appendable=appendable)
    except UpstreamError as exc:
        log.warning("Upstream unavailable for %s: %s", path, exc)
        return None
    return entry if entry.status == 200 else None


@bp.get("/versions")
def versions() -> Response:
    hub = get_hub()
    token = hub.private.token()
    if not token:
        return cached(
            "compact", "versions", "/versions", hub.settings.versions_ttl, TEXT, appendable=True
        )
    base = _upstream_entry("compact", "versions", "/versions", hub.settings.versions_ttl, True)
    entry = hub.merged.versions(base, hub.private.versions_lines(), token)
    return serve_entry(entry, TEXT)


@bp.get("/names")
def names() -> Response:
    hub = get_hub()
    token = hub.private.token()
    if not token:
        return cached("compact", "names", "/names", hub.settings.versions_ttl, TEXT)
    base = _upstream_entry("compact", "names", "/names", hub.settings.versions_ttl)
    entry = hub.merged.names(base, hub.private.live_names(), token)
    return serve_entry(entry, TEXT)


@bp.get("/info/<name>")
def info(name: str) -> Response:
    if not valid_gem_name(name):
        abort(404)
    hub = get_hub()
    # A private name always wins: it must never fall through to a public gem of that name.
    if hub.private.token() and hub.private.is_private(name):
        return _bytes_response(hub.private.info_text(name).encode())
    return cached("info", name, f"/info/{name}", hub.settings.info_ttl, TEXT)


# -- legacy index --------------------------------------------------------------


def _specs(filename: str) -> Response:
    return cached("specs", filename, f"/{filename}", get_hub().settings.specs_ttl, BINARY)


@bp.get("/specs.4.8.gz")
def specs() -> Response:
    return _specs("specs.4.8.gz")


@bp.get("/latest_specs.4.8.gz")
def latest_specs() -> Response:
    return _specs("latest_specs.4.8.gz")


@bp.get("/prerelease_specs.4.8.gz")
def prerelease_specs() -> Response:
    return _specs("prerelease_specs.4.8.gz")


@bp.get("/quick/Marshal.4.8/<filename>")
def quick_spec(filename: str) -> Response:
    if not valid_quick_filename(filename):
        abort(404)
    hub = get_hub()
    if hub.private.token() and hub.private.shadows_file(filename):
        body = hub.private.quick_spec_for(filename[: -len(".gemspec.rz")])
        if body is None:
            abort(404)
        return _bytes_response(body, BINARY)
    # A published gem version's spec never changes, hence the long TTL.
    return cached(
        "quick", filename, f"/quick/Marshal.4.8/{filename}", get_hub().settings.quick_ttl, BINARY
    )


# -- dependency API ------------------------------------------------------------


@bp.get("/api/v1/dependencies")
def dependencies() -> Response:
    raw = request.args.get("gems", "")
    wanted = sorted({part for part in raw.split(",") if part})
    if len(wanted) > 200 or len(raw) > 8192:
        abort(414, "Too many gems requested at once.")
    if any(not valid_gem_name(name) for name in wanted):
        abort(400, "Invalid gem name.")
    joined = ",".join(wanted)
    return cached(
        "deps",
        joined,
        "/api/v1/dependencies",
        get_hub().settings.info_ttl,
        BINARY,
        params={"gems": joined},
    )


# -- gem files -----------------------------------------------------------------


def _send_private(filename: str) -> Optional[Response]:
    hub = get_hub()
    if not hub.private.token() or not hub.private.shadows_file(filename):
        return None
    release = hub.private.release_for_filename(filename)
    if release is None or release.yanked:
        abort(404)
    path = hub.private.path_for(filename)
    if not path.is_file():
        log.error("Private gem file is missing from disk: %s", path)
        abort(404)
    response = send_file(path, mimetype=BINARY, conditional=True, max_age=0, download_name=filename)
    if response.status_code in (200, 206):
        hub.private.record_hit(release)
    return response


def download_gem(filename: str) -> Response:
    hub = get_hub()
    private = _send_private(filename)
    if private is not None:
        return private
    try:
        path = hub.gems.get_or_fetch(filename)
    except (InvalidFilename, GemFileNotFound):
        abort(404)
    except ChecksumMismatch as exc:
        log.error("%s", exc)
        abort(502, "Upstream sent a gem that does not match its published checksum; not cached.")
    except UpstreamError as exc:
        log.warning("Could not fetch %s: %s", filename, exc)
        abort(502, "Upstream RubyGems server is unavailable and this gem is not cached yet.")
    response = send_file(
        path,
        mimetype=BINARY,
        conditional=True,
        max_age=86400,
        download_name=filename,
    )
    if response.status_code in (200, 206):
        hub.gems.record_hit(filename, path)
    return response


@bp.get("/downloads/<filename>")
def legacy_download(filename: str) -> Response:
    return download_gem(filename)
