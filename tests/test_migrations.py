"""Alembic migrations and the key/migrate CLI."""

from __future__ import annotations

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from gemhub import models  # noqa: F401
from gemhub.cli import main
from gemhub.extensions import db
from gemhub.migrate import (
    current_revision,
    downgrade_database,
    head_revision,
    history,
    upgrade_database,
)


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    yield eng
    eng.dispose()


def test_fresh_database_reaches_head_and_matches_the_models(engine):
    upgrade_database(engine)
    assert current_revision(engine) == head_revision() == "0002"
    tables = set(inspect(engine).get_table_names())
    assert {"gems", "cached_files", "daily_downloads", "api_keys", "private_gems"} <= tables
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_upgrade_is_idempotent(engine):
    upgrade_database(engine)
    upgrade_database(engine)
    assert current_revision(engine) == "0002"


def test_a_0_1_database_is_adopted_and_keeps_its_data(engine):
    upgrade_database(engine, "0001")  # exactly the 0.1 schema
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))  # 0.1 had no version table
        conn.execute(
            text(
                "INSERT INTO gems (name, downloads, data, versions, owners, first_cached_at, "
                "fetched_at) VALUES ('rack', 5, '{}', '[]', '[]', '2026-01-01', '2026-01-01')"
            )
        )
    upgrade_database(engine)
    assert current_revision(engine) == "0002"
    with engine.connect() as conn:
        row = conn.execute(text("SELECT name, downloads, is_private FROM gems")).one()
    assert tuple(row) == ("rack", 5, 0)


def test_downgrade_and_upgrade_again(engine):
    upgrade_database(engine)
    downgrade_database(engine, "0001")
    tables = set(inspect(engine).get_table_names())
    assert "api_keys" not in tables and "gems" in tables
    assert "is_private" not in {c["name"] for c in inspect(engine).get_columns("gems")}
    upgrade_database(engine)
    assert "api_keys" in set(inspect(engine).get_table_names())


def test_history_lists_every_revision():
    lines = history()
    assert lines[0].startswith("0001") and lines[-1].startswith("0002")


def test_cli_migrate_status_and_key_lifecycle(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMHUB_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GEMHUB_LOG_LEVEL", "ERROR")
    assert main(["migrate"]) == 0
    assert "revision 0002" in capsys.readouterr().out
    assert main(["db-status"]) == 0
    assert "current: 0002" in capsys.readouterr().out

    assert main(["key", "create", "ci", "--scopes", "push"]) == 0
    out = capsys.readouterr().out
    secret = next(word for word in out.split() if word.startswith("gh_"))
    assert main(["key", "create", "ci"]) == 1  # duplicate name
    assert main(["key", "create", "bad", "--scopes", "root"]) == 1
    capsys.readouterr()
    assert main(["key", "list"]) == 0
    listing = capsys.readouterr().out
    assert "ci" in listing and "active" in listing and secret not in listing
    assert main(["key", "revoke", "ci"]) == 0
    assert main(["key", "revoke", "ci"]) == 1
    assert main(["stats"]) == 0
    assert "private releases" in capsys.readouterr().out


@pytest.mark.skipif(
    not os.environ.get("GEMHUB_TEST_DATABASE_URL"), reason="needs GEMHUB_TEST_DATABASE_URL"
)
def test_migrations_on_an_external_database():
    from conftest import reset_external_database

    url = os.environ["GEMHUB_TEST_DATABASE_URL"]
    reset_external_database(url)
    eng = create_engine(url)
    try:
        upgrade_database(eng)
        assert current_revision(eng) == "0002"
        with eng.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), db.metadata) == []
        downgrade_database(eng, "0001")
        assert "api_keys" not in set(inspect(eng).get_table_names())
        upgrade_database(eng)
        assert current_revision(eng) == "0002"
    finally:
        eng.dispose()
