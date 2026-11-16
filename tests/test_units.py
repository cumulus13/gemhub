"""Pure-function and configuration tests."""

from __future__ import annotations

import pytest

from gemhub.cache import Cache
from gemhub.config import ConfigError, Settings
from gemhub.names import split_candidates, valid_gem_filename, valid_gem_name
from gemhub.proxy import md5_from_etag
from gemhub.views import Pagination, ago, datefmt, filesize, pessimistic, safe_url, sparkline


def test_settings_defaults_and_validation(tmp_path):
    settings = Settings.from_env({"GEMHUB_DATA_DIR": str(tmp_path)})
    assert settings.upstream_url == "https://rubygems.org"
    assert settings.redis_ttl == 3600 and settings.db_ttl == 6 * 3600
    assert settings.database_url.startswith("sqlite:///")
    assert settings.host == "127.0.0.1"
    for bad in (
        {"GEMHUB_PORT": "eighty"},
        {"GEMHUB_PORT": "0"},
        {"GEMHUB_UPSTREAM_URL": "ftp://example.org"},
        {"GEMHUB_VERIFY_CHECKSUMS": "maybe"},
        {"GEMHUB_PUBLIC_URL": "hub.local"},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({"GEMHUB_DATA_DIR": str(tmp_path), **bad})


def test_legacy_postgres_scheme_is_normalised(tmp_path):
    settings = Settings.from_env(
        {"GEMHUB_DATA_DIR": str(tmp_path), "GEMHUB_DATABASE_URL": "postgres://u:p@h/db"}
    )
    assert settings.database_url == "postgresql://u:p@h/db"


def test_name_validation():
    assert (
        valid_gem_name("rails") and valid_gem_name("net-http-pipeline") and valid_gem_name("a.b_c")
    )
    for bad in ("", ".hidden", "-x", "a/b", "a..b", "a b", "x" * 300):
        assert not valid_gem_name(bad)
    assert valid_gem_filename("nokogiri-1.16.0-x86_64-linux.gem")
    assert not valid_gem_filename("../x.gem") and not valid_gem_filename("x.gemspec")


def test_split_candidates_prefers_longest_name_first():
    assert split_candidates("rails-7.1.0") == [("rails", "7.1.0")]
    assert split_candidates("net-http-pipeline-1.0.1") == [("net-http-pipeline", "1.0.1")]
    assert split_candidates("nokogiri-1.16.0-x86_64-linux") == [("nokogiri", "1.16.0-x86_64-linux")]
    assert split_candidates("foo-2-3.4") == [("foo-2", "3.4"), ("foo", "2-3.4")]
    assert split_candidates("noversion") == []


def test_md5_from_etag():
    md5 = "d41d8cd98f00b204e9800998ecf8427e"
    assert md5_from_etag(f'"{md5}"') == md5
    assert md5_from_etag(f'W/"{md5}"') == md5
    assert md5_from_etag('"not-an-md5"') is None
    assert md5_from_etag(None) is None


def test_template_helpers():
    assert pessimistic("7.1.3") == "~> 7.1"
    assert pessimistic("3") == "~> 3"
    assert pessimistic("2.0.0.rc1") == "2.0.0.rc1"
    assert safe_url("https://example.org/x") == "https://example.org/x"
    assert (
        safe_url("javascript:alert(1)") == "" and safe_url("//evil") == "" and safe_url(None) == ""
    )
    assert datefmt("2025-03-04T10:00:00.000Z") == "Mar 4, 2025"
    assert datefmt("garbage") == "garbage" and datefmt(None) == ""
    assert filesize(0) == "0 B" and filesize(1536) == "1.5 KB"
    assert ago(None) == "never"


def test_sparkline_handles_flat_and_varying_series():
    flat = sparkline([0] * 30)
    assert flat["peak"] == 0 and flat["line"].count(",") == 30
    varied = sparkline([0, 5, 10])
    assert varied["peak"] == 10 and varied["area"].startswith("3,41 ")


def test_pagination_window():
    pager = Pagination(page=10, per_page=10, total=200)
    assert list(pager.window()) == [1, None, 8, 9, 10, 11, 12, None, 20]
    assert Pagination(1, 10, 0).pages == 1 and Pagination(1, 10, 0).first_item == 0


def test_memory_cache_roundtrip_and_expiry(monkeypatch):
    cache = Cache(None)
    cache.set_json("k", {"a": 1}, 60)
    assert cache.get_json("k") == {"a": 1}
    cache.delete("k")
    assert cache.get_json("k") is None
    assert cache.ping() is None and cache.backend == "memory"
    cache.set_json("short", 1, 1)
    import time

    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 5)
    assert cache.get_json("short") is None


def test_unreachable_redis_degrades_to_memory():
    cache = Cache("redis://127.0.0.1:1/0")
    cache.set_json("k", {"ok": True}, 60)
    assert cache.get_json("k") == {"ok": True}
    assert cache.ping() is False
