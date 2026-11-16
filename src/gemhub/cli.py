"""Command line entry point: ``gemhub serve | init-db | refresh | purge | stats``."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from sqlalchemy import func, select

from . import __version__
from .app import create_app
from .auth import create_key, revoke_key
from .config import ConfigError, Settings
from .extensions import db
from .metadata import GemNotFound, UpstreamUnavailable
from .migrate import current_revision, head_revision, history, upgrade_database
from .models import ApiKey, CachedFile, Gem, PrivateRelease


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemhub", description="Caching RubyGems mirror with a web hub."
    )
    parser.add_argument("--version", action="version", version=f"gemhub {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the production server (waitress)")
    serve.add_argument("--host", help="bind address (default: GEMHUB_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default: GEMHUB_PORT or 8080)")
    serve.add_argument("--threads", type=int, help="worker threads (default: GEMHUB_THREADS or 16)")

    sub.add_parser("migrate", help="apply database migrations (also run automatically on start)")
    sub.add_parser("init-db", help="alias of migrate")
    sub.add_parser("db-status", help="show the database schema revision and migration history")

    key = sub.add_parser("key", help="manage API keys for pushing private gems")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    create = key_sub.add_parser("create", help="create a key (the secret is shown once)")
    create.add_argument("name", help="a label, e.g. ci or alice")
    create.add_argument("--scopes", default="push,yank", help="comma list of push, yank, admin")
    key_sub.add_parser("list", help="list keys (secrets are never stored)")
    revoke = key_sub.add_parser("revoke", help="revoke a key")
    revoke.add_argument("name")

    refresh = sub.add_parser("refresh", help="re-fetch gem metadata from rubygems.org now")
    refresh.add_argument("names", nargs="+", help="gem names (case-sensitive)")

    purge = sub.add_parser("purge", help="delete cached .gem files not served recently")
    purge.add_argument(
        "--older-than-days", type=int, required=True, help="idle days before removal"
    )
    purge.add_argument("--dry-run", action="store_true", help="only report what would be removed")

    sub.add_parser("stats", help="print cache statistics")
    return parser


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _key_command(args: argparse.Namespace) -> int:
    if args.key_command == "create":
        try:
            key, plain = create_key(args.name, args.scopes)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Created key {key.name!r} with scopes {key.scopes}.")
        print("Copy the secret now; it cannot be shown again:\n")
        print(f"  {plain}\n")
        print("Use it with:  GEM_HOST_API_KEY=<secret> gem push --host <hub url> my_gem.gem")
        return 0
    if args.key_command == "revoke":
        if revoke_key(args.name):
            print(f"revoked {args.name!r}")
            return 0
        print(f"no active key named {args.name!r}", file=sys.stderr)
        return 1
    rows = db.session.execute(select(ApiKey).order_by(ApiKey.id)).scalars().all()
    if not rows:
        print("no keys yet; create one with: gemhub key create NAME")
    for row in rows:
        state = "revoked" if row.revoked_at else "active"
        used = row.last_used_at.isoformat(timespec="seconds") if row.last_used_at else "never"
        print(f"{row.name:<24} {state:<8} scopes={row.scopes:<18} last used {used}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    app = create_app(settings)

    if args.command == "serve":
        from waitress import serve

        host = args.host or settings.host
        port = args.port or settings.port
        threads = args.threads or settings.threads
        print(
            f"gemhub {__version__} listening on http://{host}:{port} "
            f"(upstream {settings.upstream_url})"
        )
        serve(app, host=host, port=port, threads=threads, ident="gemhub")
        return 0

    with app.app_context():
        if args.command in ("migrate", "init-db"):
            upgrade_database(db.engine)
            print(f"database at revision {current_revision(db.engine)}")
            return 0

        if args.command == "db-status":
            print(f"current: {current_revision(db.engine)}")
            print(f"latest : {head_revision()}")
            for line in history():
                print(f"  {line}")
            return 0

        if args.command == "key":
            return _key_command(args)

        if args.command == "refresh":
            hub = app.extensions["gemhub"]
            status = 0
            for name in args.names:
                try:
                    view = hub.metadata.get_gem(name, force_refresh=True)
                    print(f"{name}: refreshed ({view.data.get('version', '?')})")
                except GemNotFound:
                    print(f"{name}: not found on upstream", file=sys.stderr)
                    status = 1
                except UpstreamUnavailable as exc:
                    print(f"{name}: upstream unavailable: {exc}", file=sys.stderr)
                    status = 1
            return status

        if args.command == "purge":
            hub = app.extensions["gemhub"]
            count, freed = hub.gems.purge(args.older_than_days, dry_run=args.dry_run)
            verb = "would remove" if args.dry_run else "removed"
            print(f"{verb} {count} file(s), {_human(freed)}")
            return 0

        if args.command == "stats":
            gems = db.session.scalar(select(func.count()).select_from(Gem)) or 0
            files = db.session.scalar(select(func.count()).select_from(CachedFile)) or 0
            size = db.session.scalar(select(func.coalesce(func.sum(CachedFile.size), 0))) or 0
            hits = db.session.scalar(select(func.coalesce(func.sum(CachedFile.hits), 0))) or 0
            print(f"gems with metadata : {gems}")
            print(f"cached .gem files  : {files} ({_human(int(size))})")
            print(f"downloads served   : {hits}")
            private = db.session.scalar(select(func.count()).select_from(PrivateRelease)) or 0
            print(f"private releases   : {private}")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
