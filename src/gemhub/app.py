"""Application factory."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from typing import Optional

from flask import Flask, Response, g, render_template, request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, views
from .cache import Cache
from .config import Settings
from .extensions import db
from .gems import GemStore
from .hub import Hub
from .merge import MergedIndex
from .metadata import MetadataService
from .migrate import upgrade_database
from .private import PrivateService
from .proxy import ProxyCache
from .routes import register_blueprints
from .upstream import Upstream

log = logging.getLogger(__name__)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
    """WAL + busy timeout so several threads and workers can share one SQLite file."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_app(settings: Optional[Settings] = None, upstream: Optional[Upstream] = None) -> Flask:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=settings.database_url,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        JSON_AS_ASCII=False,
        TEMPLATES_AUTO_RELOAD=False,
        MAX_CONTENT_LENGTH=settings.max_gem_bytes + 64 * 1024,
    )
    if settings.trust_proxy_hops:
        hops = settings.trust_proxy_hops
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_prefix=hops
        )

    db.init_app(app)
    upstream = upstream or Upstream(settings)
    cache = Cache(settings.redis_url)
    proxy = ProxyCache(settings, upstream)
    gems = GemStore(settings, upstream, proxy, cache)
    metadata = MetadataService(settings, upstream, cache)
    private = PrivateService(settings, upstream, cache)
    merged = MergedIndex(settings)
    app.extensions["gemhub"] = Hub(
        settings, upstream, cache, proxy, gems, metadata, private, merged
    )

    views.register(app)
    register_blueprints(app)
    _register_hooks(app)

    if settings.auto_migrate:
        with app.app_context():
            upgrade_database(db.engine)
    log.info(
        "gemhub %s ready: upstream=%s cache=%s data_dir=%s",
        __version__,
        settings.upstream_url,
        cache.backend,
        settings.data_dir,
    )
    return app


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def _nonce() -> None:
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _globals():
        return {"csp_nonce": g.get("csp_nonce", ""), "hub_version": __version__}

    @app.after_request
    def _security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if response.mimetype == "text/html":
            nonce = g.get("csp_nonce", "")
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
        return response

    def _wants_html() -> bool:
        return request.accept_mimetypes.best == "text/html"

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        description = exc.description or ""
        if _wants_html():
            return (
                render_template(
                    "error.html", code=exc.code, title=exc.name, description=description
                ),
                exc.code,
            )
        return Response(f"{exc.code} {exc.name}: {description}\n", exc.code, mimetype="text/plain")

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.path)
        if _wants_html():
            return (
                render_template(
                    "error.html",
                    code=500,
                    title="Something went wrong",
                    description="The hub hit an unexpected error. It has been logged.",
                ),
                500,
            )
        return Response("500 Internal Server Error\n", 500, mimetype="text/plain")
