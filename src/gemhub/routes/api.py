"""Read-only JSON API of rubygems.org, cached and passed through unchanged."""

from __future__ import annotations

from flask import Blueprint, Response, abort, jsonify, request

from ..auth import require_key
from ..hub import get_hub
from ..names import valid_gem_name
from ..private import PrivateError
from .index import cached

bp = Blueprint("api", __name__, url_prefix="/api/v1")

JSON = "application/json; charset=utf-8"


def _private_row(name: str):
    from sqlalchemy import select

    from ..extensions import db
    from ..models import Gem

    hub = get_hub()
    if not hub.private.token() or not hub.private.is_private(name):
        return None
    return db.session.execute(select(Gem).where(Gem.name == name)).scalar_one_or_none()


def _text(message: str, status: int = 200) -> Response:
    return Response(message + "\n", status, mimetype="text/plain")


@bp.post("/gems")
def push() -> Response:
    """``gem push``: the request body is the raw ``.gem`` file."""
    key = require_key("push")
    try:
        parsed = get_hub().private.push(request.get_data(cache=False), key)
    except PrivateError as exc:
        return _text(exc.message, exc.status)
    return _text(f"Successfully registered gem: {parsed.name} ({parsed.version})")


def _yank_request(yanked: bool) -> Response:
    key = require_key("yank")
    name = request.values.get("gem_name", "").strip()
    version = request.values.get("version", "").strip()
    platform = request.values.get("platform", "").strip() or "ruby"
    if not name or not version:
        return _text("gem_name and version are required.", 400)
    try:
        get_hub().private.set_yanked(name, version, platform, key, yanked)
    except PrivateError as exc:
        return _text(exc.message, exc.status)
    verb = "Successfully yanked" if yanked else "Successfully unyanked"
    return _text(f"{verb} gem: {name} ({version})")


@bp.delete("/gems/yank")
def yank() -> Response:
    return _yank_request(True)


@bp.put("/gems/unyank")
def unyank() -> Response:
    return _yank_request(False)


@bp.get("/gems/<name>.json")
def gem(name: str) -> Response:
    if not valid_gem_name(name):
        abort(404)
    row = _private_row(name)
    if row is not None:
        return jsonify(row.data)
    return cached(
        "api", f"gem:{name}", f"/api/v1/gems/{name}.json", get_hub().settings.redis_ttl, JSON
    )


@bp.get("/versions/<name>.json")
def gem_versions(name: str) -> Response:
    if not valid_gem_name(name):
        abort(404)
    row = _private_row(name)
    if row is not None:
        return jsonify(row.versions)
    return cached(
        "api",
        f"versions:{name}",
        f"/api/v1/versions/{name}.json",
        get_hub().settings.redis_ttl,
        JSON,
    )


@bp.get("/search.json")
def search() -> Response:
    query = request.args.get("query", "").strip()
    if not query or len(query) > 100:
        abort(400, "Provide a search query of 1 to 100 characters.")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        abort(400, "page must be an integer.")
    if page > 1000:
        abort(400, "page is too large.")
    return cached(
        "api",
        f"search:{page}:{query.lower()}",
        "/api/v1/search.json",
        get_hub().settings.redis_ttl,
        JSON,
        params={"query": query, "page": page},
    )
