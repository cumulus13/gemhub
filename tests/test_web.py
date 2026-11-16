"""Web hub: metadata cache layers, pages, pagination, safety."""

from __future__ import annotations

import html as htmllib
import re
from datetime import timedelta

from sqlalchemy import select

from gemhub.extensions import db
from gemhub.models import Gem, utcnow


def test_gem_page_renders_upstream_data(client, upstream):
    upstream.add_gem("foo", "1.2.3", downloads=4_200_000, info="Does foo things")
    response = client.get("/gems/foo")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Does foo things" in html
    assert "1.2.3" in html
    assert "4,200,000" in html  # all-time downloads from rubygems.org
    assert 'gem "foo", "~> 1.2"' in htmllib.unescape(html)
    assert "rack" in html and "rake" in html  # runtime and development dependencies
    assert "https://example.org/foo" in html
    assert "ada" in html  # owners


def test_metadata_flows_redis_layer_then_database_then_upstream(app, client, upstream, hub):
    upstream.add_gem("foo", "1.0.0")
    client.get("/gems/foo")
    assert upstream.hits["/api/v1/gems/foo.json"] == 1

    client.get("/gems/foo")  # answered by the short-lived cache
    assert upstream.hits["/api/v1/gems/foo.json"] == 1

    hub.cache.delete("gem:foo")  # cache expired, database still fresh
    client.get("/gems/foo")
    assert upstream.hits["/api/v1/gems/foo.json"] == 1

    with app.app_context():  # database copy older than the 6 hour freshness window
        row = db.session.execute(select(Gem)).scalar_one()
        row.fetched_at = utcnow() - timedelta(hours=7)
        db.session.commit()
    hub.cache.delete("gem:foo")
    client.get("/gems/foo")
    assert upstream.hits["/api/v1/gems/foo.json"] == 2


def test_stale_metadata_is_served_when_upstream_fails(app, client, upstream, hub):
    upstream.add_gem("foo", "1.0.0")
    client.get("/gems/foo")
    with app.app_context():
        row = db.session.execute(select(Gem)).scalar_one()
        row.fetched_at = utcnow() - timedelta(hours=9)
        db.session.commit()
    hub.cache.delete("gem:foo")
    upstream.fail = True
    response = client.get("/gems/foo")
    assert response.status_code == 200
    assert "could not be reached" in response.get_data(as_text=True)


def test_unknown_gem_is_404_page_and_502_when_upstream_down(client, upstream):
    response = client.get("/gems/ghost", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "ghost" in response.get_data(as_text=True)
    upstream.fail = True
    down = client.get("/gems/other", headers={"Accept": "text/html"})
    assert down.status_code == 502
    assert "could not be reached" in down.get_data(as_text=True)


def test_gem_deleted_upstream_is_dropped_from_the_list(app, client, upstream, hub):
    upstream.add_gem("foo", "1.0.0")
    client.get("/gems/foo")
    upstream.gems.clear()
    hub.cache.delete("gem:foo")
    with app.app_context():
        row = db.session.execute(select(Gem)).scalar_one()
        row.fetched_at = utcnow() - timedelta(hours=9)
        db.session.commit()
    assert client.get("/gems/foo").status_code == 404
    with app.app_context():
        assert db.session.execute(select(Gem)).first() is None


def test_home_lists_cached_gems_with_pagination_and_sorting(make_settings):
    from gemhub.app import create_app

    app = create_app(make_settings(PAGE_SIZE=10))
    with app.app_context():
        for i in range(25):
            db.session.add(
                Gem(
                    name=f"gem{i:02d}",
                    latest_version="1.0.0",
                    summary=f"summary {i}",
                    downloads=i * 10,
                    data={},
                    versions=[],
                    owners=[],
                )
            )
        db.session.commit()
    client = app.test_client()

    page1 = client.get("/").get_data(as_text=True)
    assert page1.count('class="row"') == 10
    assert "gem24" in page1 and "gem14" not in page1  # most downloaded first
    page3 = client.get("/?page=3").get_data(as_text=True)
    assert page3.count('class="row"') == 5
    assert "Showing 21 to 25 of 25" in page3
    clamped = client.get("/?page=999").get_data(as_text=True)
    assert "Showing 21 to 25 of 25" in clamped
    by_name = client.get("/?sort=name").get_data(as_text=True)
    assert by_name.index("gem00") < by_name.index("gem09")
    assert client.get("/?page=abc&sort=bogus").status_code == 200


def test_empty_home_shows_setup_help(client):
    html = client.get("/").get_data(as_text=True)
    assert "Nothing cached yet" in html
    assert "bundle config set --global mirror.https://rubygems.org" in html


def test_search_marks_gems_already_in_the_hub(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    upstream.add_gem("foobar", "2.0.0")
    client.get("/gems/foo")
    html = client.get("/search?query=foo").get_data(as_text=True)
    assert "foobar" in html
    assert "in this hub" in html and "not cached yet" in html
    assert client.get("/search?query=").status_code == 302
    assert client.get("/search?query=" + "x" * 101).status_code == 400


def test_versions_page_lists_every_release(client, upstream):
    upstream.add_gem("foo", "1.0.0")
    upstream.add_gem("foo", "1.1.0")
    html = client.get("/gems/foo/versions").get_data(as_text=True)
    assert "1.0.0" in html and "1.1.0" in html


def test_untrusted_metadata_cannot_inject_script_or_javascript_links(client, upstream):
    upstream.add_gem(
        "evil",
        "1.0.0",
        info="<script>alert(1)</script>",
        homepage_uri="javascript:alert(1)",
        source_code_uri="https://example.org/src",
    )
    html = client.get("/gems/evil").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:" not in html
    assert "https://example.org/src" in html


def test_html_responses_carry_a_nonce_based_csp(client):
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in response.get_data(as_text=True)
    assert "script-src 'self' 'nonce-" in csp and "unsafe-inline" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in client.get("/healthz").headers


def test_ops_endpoints(client, upstream):
    assert client.get("/healthz").get_json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.get_json()["checks"] == {"database": "ok", "redis": "not configured"}
    upstream.add_gem("foo", "1.0.0")
    client.get("/gems/foo")
    stats = client.get("/_hub/stats").get_json()
    assert stats["gems"] == 1 and stats["cache_backend"] == "memory"
    assert "Disallow: /info/" in client.get("/robots.txt").get_data(as_text=True)


def test_non_html_clients_get_plain_text_errors(client):
    response = client.get("/info/unknown-gem", headers={"Accept": "*/*"})
    assert response.status_code in (404, 502)
    assert response.mimetype == "text/plain"
