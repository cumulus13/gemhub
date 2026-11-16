"""Private gems: push, auth, shadowing, index merging, yank, web pages."""

from __future__ import annotations

import hashlib
import io
import zlib

import pytest
from sqlalchemy import select

from gemhub.app import create_app
from gemhub.auth import create_key
from gemhub.extensions import db
from gemhub.models import Gem, PrivateRelease
from gemtools import build_gem


@pytest.fixture()
def keys(app):
    with app.app_context():
        return {
            "owner": create_key("owner", "push,yank")[1],
            "other": create_key("other", "push,yank")[1],
            "admin": create_key("admin", "admin")[1],
            "pushonly": create_key("pushonly", "push")[1],
        }


def push(client, key, data, **kw):
    headers = {"Content-Type": "application/octet-stream"}
    if key:
        headers["Authorization"] = key
    return client.post("/api/v1/gems", data=data, headers=headers, **kw)


def test_push_requires_a_valid_key_with_the_right_scope(client, keys):
    gem = build_gem("acme", "1.0.0")
    assert push(client, None, gem).status_code == 401
    assert push(client, "gh_wrong", gem).status_code == 401
    yank = client.delete(
        "/api/v1/gems/yank",
        data={"gem_name": "acme", "version": "1.0.0"},
        headers={"Authorization": keys["pushonly"]},
    )
    assert yank.status_code == 403


def test_pushed_gem_appears_in_the_compact_index_and_downloads(client, upstream, keys):
    upstream.add_gem("rack", "3.0.0")
    gem = build_gem("acme", "1.2.0", deps=[("rack", ">=", "2.0", "runtime")])
    response = push(client, keys["owner"], gem)
    assert response.status_code == 200, response.data
    assert b"Successfully registered gem: acme (1.2.0)" in response.data

    sha = hashlib.sha256(gem).hexdigest()
    info = client.get("/info/acme").get_data(as_text=True)
    assert info == f"---\n1.2.0 rack:>= 2.0|checksum:{sha},ruby:>= 2.5.0\n"

    names = client.get("/names").get_data(as_text=True).splitlines()
    assert names == ["---", "acme", "rack"]

    versions = client.get("/versions").get_data(as_text=True)
    assert "rack 3.0.0 " in versions  # upstream content is preserved
    info_md5 = hashlib.md5(info.encode(), usedforsecurity=False).hexdigest()
    assert f"acme 1.2.0 {info_md5}" in versions.splitlines()

    download = client.get("/gems/acme-1.2.0.gem")
    assert download.status_code == 200 and download.data == gem
    assert upstream.hits["/gems/acme-1.2.0.gem"] == 0

    spec = zlib.decompress(client.get("/quick/Marshal.4.8/acme-1.2.0.gemspec.rz").data)
    assert spec.startswith(b"\x04\x08u:\x17Gem::Specification")
    assert b"acme" in spec and b"rack" in spec


def test_versions_etag_matches_served_bytes_and_ranges_work(client, upstream, keys):
    upstream.add_gem("rack", "3.0.0")
    push(client, keys["owner"], build_gem("acme"))
    full = client.get("/versions")
    assert full.headers["ETag"] == f'"{hashlib.md5(full.data, usedforsecurity=False).hexdigest()}"'
    assert client.get("/versions", headers={"Range": "bytes=5-"}).data == full.data[5:]
    assert (
        client.get("/versions", headers={"If-None-Match": full.headers["ETag"]}).status_code == 304
    )


def test_duplicate_version_and_ownership(client, keys):
    gem = build_gem("acme", "1.0.0")
    assert push(client, keys["owner"], gem).status_code == 200
    assert push(client, keys["owner"], gem).status_code == 409
    newer = build_gem("acme", "1.1.0")
    assert push(client, keys["other"], newer).status_code == 403
    assert push(client, keys["admin"], newer).status_code == 200
    assert push(client, keys["owner"], build_gem("acme", "1.2.0")).status_code == 200


def test_public_names_cannot_be_hijacked_by_default(client, upstream, keys):
    upstream.add_gem("rails", "8.0.0")
    refused = push(client, keys["owner"], build_gem("rails", "9.9.9"))
    assert refused.status_code == 409
    assert b"dependency confusion" in refused.data
    assert client.get("/info/rails").get_data(as_text=True).count("checksum") == 1  # still public
    upstream.fail = True
    assert push(client, keys["owner"], build_gem("fresh")).status_code == 503


def test_allow_policy_lets_a_private_gem_shadow_a_public_one(make_settings, upstream):
    app = create_app(make_settings(PRIVATE_NAME_POLICY="allow"))
    client = app.test_client()
    with app.app_context():
        key = create_key("k")[1]
    upstream.add_gem("rails", "8.0.0")
    assert push(client, key, build_gem("rails", "9.9.9")).status_code == 200
    info = client.get("/info/rails").get_data(as_text=True)
    assert "9.9.9" in info and "8.0.0" not in info
    before = upstream.hits["/gems/rails-8.0.0.gem"]
    assert client.get("/gems/rails-8.0.0.gem").status_code == 404  # never falls through
    assert upstream.hits["/gems/rails-8.0.0.gem"] == before


def test_unknown_version_of_a_private_gem_does_not_reach_upstream(client, upstream, keys):
    push(client, keys["owner"], build_gem("acme", "1.0.0"))
    assert client.get("/gems/acme-9.9.9.gem").status_code == 404
    assert client.get("/quick/Marshal.4.8/acme-9.9.9.gemspec.rz").status_code == 404
    assert upstream.hits["/gems/acme-9.9.9.gem"] == 0


def test_yank_and_unyank(client, upstream, keys):
    push(client, keys["owner"], build_gem("acme", "1.0.0"))
    push(client, keys["owner"], build_gem("acme", "1.1.0"))
    auth = {"Authorization": keys["owner"]}
    yanked = client.delete(
        "/api/v1/gems/yank", data={"gem_name": "acme", "version": "1.1.0"}, headers=auth
    )
    assert yanked.status_code == 200
    info = client.get("/info/acme").get_data(as_text=True)
    assert "1.0.0" in info and "1.1.0" not in info
    assert client.get("/gems/acme-1.1.0.gem").status_code == 404
    assert (
        client.delete(
            "/api/v1/gems/yank", data={"gem_name": "acme", "version": "1.1.0"}, headers=auth
        ).status_code
        == 422
    )
    back = client.put(
        "/api/v1/gems/unyank", data={"gem_name": "acme", "version": "1.1.0"}, headers=auth
    )
    assert back.status_code == 200
    assert "1.1.0" in client.get("/info/acme").get_data(as_text=True)
    other = client.delete(
        "/api/v1/gems/yank",
        data={"gem_name": "acme", "version": "1.0.0"},
        headers={"Authorization": keys["other"]},
    )
    assert other.status_code == 403
    # a yanked version can never be pushed again
    client.delete("/api/v1/gems/yank", data={"gem_name": "acme", "version": "1.0.0"}, headers=auth)
    assert push(client, keys["owner"], build_gem("acme", "1.0.0")).status_code == 409


def test_private_gems_still_served_when_upstream_is_down(client, upstream, keys):
    push(client, keys["owner"], build_gem("acme", "1.0.0"))
    upstream.fail = True
    versions = client.get("/versions")
    assert versions.status_code == 200 and b"acme 1.0.0 " in versions.data
    assert client.get("/names").get_data(as_text=True) == "---\nacme\n"
    assert client.get("/gems/acme-1.0.0.gem").status_code == 200


@pytest.mark.parametrize(
    "data,status",
    [
        (b"", 400),
        (b"this is not a gem", 422),
        (build_gem("nodata", include_data=False), 422),
        (build_gem("badsums", break_checksums=True), 422),
        (build_gem("native", platform="x86_64-linux"), 422),
        (build_gem("..sneaky"), 422),
    ],
)
def test_bad_uploads_are_rejected(client, keys, data, status):
    assert push(client, keys["owner"], data).status_code == status


def test_oversized_upload_is_rejected(make_settings):
    app = create_app(make_settings(MAX_GEM_BYTES=2048))
    client = app.test_client()
    with app.app_context():
        key = create_key("k")[1]
    big = build_gem("big", description="x" * 5000)
    assert push(client, key, big).status_code == 413


def test_web_pages_show_private_gems(client, upstream, keys):
    upstream.add_gem("acmeish", "1.0.0")
    push(client, keys["owner"], build_gem("acme", "2.0.0", summary="Internal billing client"))
    home = client.get("/").get_data(as_text=True)
    assert "acme" in home and ", private" in home
    page = client.get("/gems/acme").get_data(as_text=True)
    assert "private" in page and "2.0.0" in page
    assert "All time on rubygems.org" not in page
    assert "It is not on rubygems.org" in page
    assert upstream.hits["/api/v1/gems/acme.json"] == 0
    results = client.get("/search?query=acme").get_data(as_text=True)
    assert "acmeish" in results and ", private" in results
    api = client.get("/api/v1/gems/acme.json").get_json()
    assert api["name"] == "acme" and api["version"] == "2.0.0"
    assert client.get("/api/v1/versions/acme.json").get_json()[0]["number"] == "2.0.0"


def test_download_counts_are_tracked(app, client, keys):
    push(client, keys["owner"], build_gem("acme", "1.0.0"))
    client.get("/gems/acme-1.0.0.gem")
    client.get("/gems/acme-1.0.0.gem")
    with app.app_context():
        assert db.session.execute(select(PrivateRelease.downloads)).scalar_one() == 2
        assert db.session.execute(select(Gem.downloads).where(Gem.name == "acme")).scalar_one() == 2


def test_revoked_keys_stop_working(app, client, keys):
    from gemhub.auth import revoke_key

    with app.app_context():
        assert revoke_key("owner") is True
        assert revoke_key("owner") is False
    assert push(client, keys["owner"], build_gem("acme")).status_code == 401


def test_marshal_writer_roundtrips_through_gemfile_parser():
    from gemhub.gemfile import parse_gem

    parsed = parse_gem(build_gem("acme", "1.0.0", deps=[("rack", ">=", "2.0", "runtime")]))
    assert parsed.name == "acme" and parsed.filename == "acme-1.0.0.gem"
    assert parsed.dependencies[0]["requirements"] == [(">=", "2.0")]
    assert io.BytesIO(b"").read() == b""
