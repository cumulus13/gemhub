"""Protocol behaviour: compact index, gem files, validation, resilience."""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from gemhub.extensions import db
from gemhub.models import CachedFile, DailyDownload


def test_gem_download_is_cached_verified_and_counted(app, client, upstream, hub):
    content = upstream.add_gem("foo", "1.0.0")
    first = client.get("/gems/foo-1.0.0.gem")
    second = client.get("/gems/foo-1.0.0.gem")
    assert first.status_code == second.status_code == 200
    assert first.data == second.data == content
    assert upstream.hits["/gems/foo-1.0.0.gem"] == 1  # second request never left the hub
    with app.app_context():
        row = db.session.execute(select(CachedFile)).scalar_one()
        assert row.gem_name == "foo"
        assert row.verified is True
        assert row.sha256 == hashlib.sha256(content).hexdigest()
        assert row.hits == 2
        daily = db.session.execute(select(DailyDownload)).scalar_one()
        assert (daily.gem_name, daily.count) == ("foo", 2)


def test_conditional_get_on_gem_file_returns_304_and_is_not_counted(app, client, upstream):
    upstream.add_gem("foo", "1.0.0")
    first = client.get("/gems/foo-1.0.0.gem")
    etag = first.headers["ETag"]
    again = client.get("/gems/foo-1.0.0.gem", headers={"If-None-Match": etag})
    assert again.status_code == 304
    with app.app_context():
        assert db.session.execute(select(CachedFile.hits)).scalar_one() == 1


def test_checksum_mismatch_is_rejected_and_not_stored(app, client, upstream, hub):
    upstream.add_gem("foo", "1.0.0")
    upstream.corrupt_gems = True
    response = client.get("/gems/foo-1.0.0.gem")
    assert response.status_code == 502
    assert b"checksum" in response.data
    assert not hub.gems.path_for("foo-1.0.0.gem").exists()
    with app.app_context():
        assert db.session.execute(select(CachedFile)).first() is None


def test_gem_with_hyphenated_name_is_attributed_correctly(app, client, upstream):
    upstream.add_gem("net-http-pipeline", "1.0.1")
    assert client.get("/gems/net-http-pipeline-1.0.1.gem").status_code == 200
    with app.app_context():
        assert db.session.execute(select(CachedFile.gem_name)).scalar_one() == "net-http-pipeline"


def test_missing_gem_is_404_and_negatively_cached(client, upstream):
    assert client.get("/gems/ghost-1.0.0.gem").status_code == 404
    assert client.get("/gems/ghost-1.0.0.gem").status_code == 404
    assert upstream.hits["/gems/ghost-1.0.0.gem"] == 1


def test_invalid_names_never_reach_upstream(client, upstream):
    for path in (
        "/gems/.hidden-1.0.gem",
        "/gems/a..b-1.0.gem",
        "/info/..",
        "/info/.secret",
        "/quick/Marshal.4.8/../x.gemspec.rz",
        "/downloads/%2e%2e%2fetc.gem",
    ):
        assert client.get(path).status_code in (400, 404), path
    assert sum(upstream.hits.values()) == 0


def test_info_and_names_are_served_with_upstream_validators(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    info = client.get("/info/foo")
    assert info.status_code == 200
    assert info.data.startswith(b"---\n1.0.0 rack:>= 2.0|checksum:")
    assert info.headers["ETag"].startswith('"') and len(info.headers["ETag"]) == 34
    assert client.get("/names").data.splitlines() == [b"---", b"foo"]
    assert client.get("/info/unknown").status_code == 404


def test_versions_is_updated_with_a_verified_ranged_request(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    first = client.get("/versions")
    assert first.data == upstream.versions_body()
    upstream.add_gem("bar", "2.0.0")
    second = client.get("/versions")
    assert second.data == upstream.versions_body()
    assert upstream.range_headers, "expected the hub to ask upstream for only the tail"
    assert second.headers["ETag"] == client.get("/versions").headers["ETag"]


def test_versions_falls_back_to_full_download_when_upstream_rewrites_history(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    client.get("/versions")
    upstream.versions_override = (
        b"created_at: 2026-02-02T00:00:00Z\n---\nfoo 1.0.0,1.0.1 deadbeef\nbar 2.0.0 cafe\n"
    )
    response = client.get("/versions")
    assert response.data == upstream.versions_override


def test_client_range_and_conditional_requests_on_versions(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    full = client.get("/versions")
    partial = client.get("/versions", headers={"Range": "bytes=10-"})
    assert partial.status_code == 206
    assert partial.data == full.data[10:]
    assert partial.headers["Content-Range"].startswith("bytes 10-")
    unchanged = client.get("/versions", headers={"If-None-Match": full.headers["ETag"]})
    assert unchanged.status_code == 304


def test_ttl_avoids_repeated_upstream_calls(make_settings, upstream):
    from gemhub.app import create_app

    app = create_app(make_settings(INFO_TTL=300))
    client = app.test_client()
    upstream.add_gem("foo", "1.0.0")
    for _ in range(3):
        assert client.get("/info/foo").status_code == 200
    assert upstream.hits["/info/foo"] == 1


def test_stale_copy_is_served_when_upstream_is_down(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    assert client.get("/info/foo").status_code == 200
    upstream.fail = True
    stale = client.get("/info/foo")
    assert stale.status_code == 200
    assert "stale" in stale.headers["Warning"]


def test_502_when_upstream_is_down_and_nothing_is_cached(client, upstream):
    upstream.fail = True
    response = client.get("/info/foo")
    assert response.status_code == 502
    assert b"unavailable" in response.data
    assert client.get("/gems/foo-1.0.0.gem").status_code == 502


def test_legacy_specs_are_passed_through(client, upstream):
    response = client.get("/specs.4.8.gz")
    assert response.status_code == 200
    assert response.data == b"\x1f\x8bfake"


def test_eviction_removes_least_recently_used_files(make_settings, upstream):
    from gemhub.app import create_app

    app = create_app(make_settings(CACHE_MAX_BYTES=1500))
    client = app.test_client()
    hub = app.extensions["gemhub"]
    for name in ("aaa", "bbb", "ccc"):
        upstream.add_gem(name, "1.0.0", content=b"x" * 700)
    for name in ("aaa", "bbb", "ccc"):
        assert client.get(f"/gems/{name}-1.0.0.gem").status_code == 200
    assert not hub.gems.path_for("aaa-1.0.0.gem").exists()
    assert hub.gems.path_for("ccc-1.0.0.gem").exists()
    with app.app_context():
        assert hub.gems.total_bytes() <= 1500


def test_purge_removes_idle_files(app, client, upstream, hub):
    upstream.add_gem("foo", "1.0.0")
    client.get("/gems/foo-1.0.0.gem")
    with app.app_context():
        assert hub.gems.purge(30, dry_run=True) == (0, 0)
        assert hub.gems.purge(0, dry_run=True)[0] in (0, 1)
        row = db.session.execute(select(CachedFile)).scalar_one()
        from datetime import timedelta

        row.last_access_at = row.last_access_at - timedelta(days=45)
        db.session.commit()
        count, freed = hub.gems.purge(30)
        assert count == 1 and freed > 0
    assert not hub.gems.path_for("foo-1.0.0.gem").exists()


def test_api_json_is_passed_through_unchanged(client, upstream):
    upstream.add_gem("foo", "1.0.0", downloads=99)
    body = client.get("/api/v1/gems/foo.json").get_json()
    assert body["name"] == "foo" and body["downloads"] == 99
    assert client.get("/api/v1/versions/foo.json").get_json()[0]["number"] == "1.0.0"
    assert client.get("/api/v1/search.json?query=fo").get_json()[0]["name"] == "foo"
    assert client.get("/api/v1/search.json").status_code == 400
    assert client.get("/api/v1/gems/nope.json").status_code == 404
