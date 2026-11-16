"""Run Alembic migrations programmatically (no ``alembic.ini`` needed)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)

BASELINE = "0001"
PG_LOCK_ID = 727_274_001
MYSQL_LOCK_NAME = "gemhub_migrate"


def _config(connection: Optional[Connection] = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", "driver://unused")
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(_config()).get_current_head() or ""


def current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def history() -> List[str]:
    script = ScriptDirectory.from_config(_config())
    return [f"{rev.revision}: {rev.doc}" for rev in script.walk_revisions()][::-1]


def _lock(conn: Connection) -> None:
    """Serialise concurrent starters so two workers never migrate at once."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": PG_LOCK_ID})
    elif conn.dialect.name in ("mysql", "mariadb"):
        conn.execute(text("SELECT GET_LOCK(:name, 60)"), {"name": MYSQL_LOCK_NAME})


def _unlock(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": PG_LOCK_ID})
    elif conn.dialect.name in ("mysql", "mariadb"):
        conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": MYSQL_LOCK_NAME})


def upgrade_database(engine: Engine, target: str = "head") -> None:
    """Bring the schema to ``target``.

    A database created by gemhub 0.1 (``create_all``, no version table) is adopted by
    stamping it at the baseline revision first, so its data is kept.
    """
    with engine.connect() as lock_conn:
        _lock(lock_conn)
        lock_conn.commit()
        try:
            tables = set(inspect(engine).get_table_names())
            with engine.begin() as conn:
                cfg = _config(conn)
                if "alembic_version" not in tables and "gems" in tables:
                    log.info("Adopting an existing 0.1 database at revision %s", BASELINE)
                    command.stamp(cfg, BASELINE)
                command.upgrade(cfg, target)
        finally:
            _unlock(lock_conn)
            lock_conn.commit()


def downgrade_database(engine: Engine, target: str) -> None:
    with engine.begin() as conn:
        command.downgrade(_config(conn), target)
