from __future__ import annotations

import os

import pytest

from fake_upstream import FakeUpstream
from gemhub.app import create_app
from gemhub.config import Settings


@pytest.fixture()
def upstream():
    server = FakeUpstream().start()
    yield server
    server.stop()


def reset_external_database(url: str) -> None:
    """Empty a shared test database (PostgreSQL, MySQL) so every test starts clean."""
    from sqlalchemy import create_engine, text

    from gemhub import models  # noqa: F401
    from gemhub.extensions import db

    engine = create_engine(url)
    try:
        db.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()


@pytest.fixture()
def make_settings(tmp_path, upstream):
    external = os.environ.get("GEMHUB_TEST_DATABASE_URL")
    if external:
        reset_external_database(external)

    def build(**overrides):
        env = {
            "GEMHUB_DATA_DIR": str(tmp_path / "data"),
            "GEMHUB_UPSTREAM_URL": upstream.url,
            "GEMHUB_UPSTREAM_RETRIES": "0",
            "GEMHUB_VERSIONS_TTL": "0",
            "GEMHUB_INFO_TTL": "0",
            "GEMHUB_LOG_LEVEL": "WARNING",
        }
        if external:
            env["GEMHUB_DATABASE_URL"] = external
        env.update({f"GEMHUB_{k}": str(v) for k, v in overrides.items()})
        return Settings.from_env(env)

    return build


@pytest.fixture()
def app(make_settings):
    return create_app(make_settings())


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def hub(app):
    return app.extensions["gemhub"]
