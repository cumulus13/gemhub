"""Health, readiness and statistics endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, Response, jsonify
from sqlalchemy import func, select, text

from .. import __version__
from ..extensions import db
from ..hub import get_hub
from ..models import CachedFile, Gem

log = logging.getLogger(__name__)
bp = Blueprint("ops", __name__)

ROBOTS = (
    "User-agent: *\nDisallow: /gems/*.gem\nDisallow: /info/\nDisallow: /quick/\nDisallow: /api/\n"
)


def collect_stats() -> Dict[str, Any]:
    hub = get_hub()
    cached = hub.cache.get_json("stats")
    if cached:
        return cached
    stats = {
        "gems": int(db.session.scalar(select(func.count()).select_from(Gem)) or 0),
        "files": int(db.session.scalar(select(func.count()).select_from(CachedFile)) or 0),
        "bytes": int(db.session.scalar(select(func.coalesce(func.sum(CachedFile.size), 0))) or 0),
        "hub_downloads": int(
            db.session.scalar(select(func.coalesce(func.sum(CachedFile.hits), 0))) or 0
        ),
    }
    hub.cache.set_json("stats", stats, 30)
    return stats


@bp.get("/healthz")
def healthz() -> Response:
    """Liveness: the process answers. Does not touch any dependency."""
    return jsonify(status="ok", version=__version__)


@bp.get("/readyz")
def readyz():
    """Readiness: the database answers and, when configured, Redis does too."""
    checks: Dict[str, Any] = {}
    ok = True
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.warning("Readiness: database check failed: %s", exc)
        checks["database"] = "unavailable"
        ok = False
    redis_state = get_hub().cache.ping()
    if redis_state is None:
        checks["redis"] = "not configured"
    elif redis_state:
        checks["redis"] = "ok"
    else:
        # Redis is an optimisation; the hub degrades to an in-process cache without it.
        checks["redis"] = "unavailable (degraded)"
    response = jsonify(status="ok" if ok else "unavailable", checks=checks)
    response.status_code = 200 if ok else 503
    return response


@bp.get("/_hub/stats")
def stats() -> Response:
    hub = get_hub()
    return jsonify(
        version=__version__,
        upstream=hub.settings.upstream_url,
        cache_backend=hub.cache.backend,
        redis_ttl_seconds=hub.settings.redis_ttl,
        database_ttl_seconds=hub.settings.db_ttl,
        **collect_stats(),
    )


@bp.get("/robots.txt")
def robots() -> Response:
    return Response(ROBOTS, mimetype="text/plain")
