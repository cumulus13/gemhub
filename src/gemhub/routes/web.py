"""The browsable web hub."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Tuple

from flask import Blueprint, abort, redirect, render_template, request, url_for
from sqlalchemy import func, select

from ..extensions import db
from ..hub import get_hub
from ..metadata import GemNotFound, UpstreamUnavailable
from ..models import CachedFile, DailyDownload, Gem, PrivateRelease, utcnow
from ..views import Pagination, safe_url
from .index import download_gem
from .ops import collect_stats

bp = Blueprint("web", __name__)

SORTS = {
    "downloads": Gem.downloads.desc(),
    "name": Gem.name.asc(),
    "recent": Gem.first_cached_at.desc(),
}
SORT_LABELS = (("downloads", "Most downloaded"), ("name", "Name"), ("recent", "Recently added"))
LINK_FIELDS = (
    ("Homepage", "homepage_uri"),
    ("Source code", "source_code_uri"),
    ("Documentation", "documentation_uri"),
    ("Changelog", "changelog_uri"),
    ("Bug tracker", "bug_tracker_uri"),
    ("Wiki", "wiki_uri"),
    ("Mailing list", "mailing_list_uri"),
    ("Funding", "funding_uri"),
    ("On rubygems.org", "project_uri"),
)


def _page_arg() -> int:
    try:
        return max(1, min(int(request.args.get("page", "1")), 1_000_000))
    except ValueError:
        return 1


def hub_url() -> str:
    configured = get_hub().settings.public_url
    return configured or request.host_url.rstrip("/")


@bp.get("/")
def home():
    hub = get_hub()
    sort = request.args.get("sort", "downloads")
    if sort not in SORTS:
        sort = "downloads"
    total = int(db.session.scalar(select(func.count()).select_from(Gem)) or 0)
    pagination = Pagination(_page_arg(), hub.settings.page_size, total)
    page = min(pagination.page, pagination.pages)
    pagination = Pagination(page, hub.settings.page_size, total)
    rows = db.session.execute(
        select(
            Gem.name,
            Gem.latest_version,
            Gem.summary,
            Gem.downloads,
            Gem.fetched_at,
            Gem.is_private,
        )
        .order_by(SORTS[sort], Gem.name.asc())
        .limit(pagination.per_page)
        .offset((page - 1) * pagination.per_page)
    ).all()
    return render_template(
        "home.html",
        rows=rows,
        pagination=pagination,
        sort=sort,
        sorts=SORT_LABELS,
        stats=collect_stats(),
        hub_url=hub_url(),
        aging_before=utcnow() - timedelta(seconds=hub.settings.db_ttl),
    )


@bp.get("/search")
def search():
    query = request.args.get("query", "").strip()
    if not query:
        return redirect(url_for("web.home"))
    if len(query) > 100:
        abort(400, "Search queries are limited to 100 characters.")
    page = _page_arg()
    hub = get_hub()
    try:
        results = hub.metadata.search(query, page)
    except UpstreamUnavailable:
        results = None
    if results is None:
        local = hub.private.search(query) if page == 1 else []
        if not local:
            abort(
                502, "rubygems.org could not be reached, so there are no search results right now."
            )
        results = []
    private = hub.private.search(query) if page == 1 else []
    seen = {r["name"] for r in private}
    results = private + [r for r in results if r.get("name") not in seen]
    names = [r["name"] for r in results if r.get("name")]
    in_hub = set()
    if names:
        in_hub = set(db.session.execute(select(Gem.name).where(Gem.name.in_(names))).scalars())
    return render_template("search.html", query=query, results=results, page=page, in_hub=in_hub)


def _hub_activity(name: str, private: bool = False) -> Dict[str, Any]:
    source = PrivateRelease.downloads if private else CachedFile.hits
    owner = PrivateRelease.gem_name if private else CachedFile.gem_name
    total = int(
        db.session.scalar(select(func.coalesce(func.sum(source), 0)).where(owner == name)) or 0
    )
    files = int(
        db.session.scalar(
            select(func.count()).select_from(CachedFile).where(CachedFile.gem_name == name)
        )
        or 0
    )
    today = utcnow().date()
    start = today - timedelta(days=29)
    counts = dict(
        db.session.execute(
            select(DailyDownload.day, DailyDownload.count).where(
                DailyDownload.gem_name == name, DailyDownload.day >= start
            )
        ).all()
    )
    series = [int(counts.get(start + timedelta(days=i), 0)) for i in range(30)]
    return {"total": total, "files": files, "series": series, "last_30": sum(series)}


@bp.get("/gems/<name>")
def gem_page(name: str):
    if name.endswith(".gem"):
        return download_gem(name)
    view = _load(name)
    data = view.data
    deps = data.get("dependencies") or {}
    links: List[Tuple[str, str]] = []
    for label, key in LINK_FIELDS:
        url = safe_url(data.get(key))
        if url:
            links.append((label, url))
    recent = view.versions[:8]
    max_dl = max([int(v.get("downloads_count") or 0) for v in recent] or [0])
    return render_template(
        "gem.html",
        view=view,
        data=data,
        runtime=deps.get("runtime") or [],
        development=deps.get("development") or [],
        links=links,
        recent=recent,
        total_versions=len(view.versions),
        max_dl=max_dl,
        ruby_required=(view.versions[0].get("ruby_version") if view.versions else None),
        activity=_hub_activity(name, view.private),
        hub_url=hub_url(),
    )


@bp.get("/gems/<name>/versions")
def gem_versions(name: str):
    view = _load(name)
    versions = view.versions
    max_dl = max([int(v.get("downloads_count") or 0) for v in versions] or [0])
    return render_template("versions.html", view=view, versions=versions, max_dl=max_dl)


def _load(name: str):
    try:
        return get_hub().metadata.get_gem(name)
    except GemNotFound:
        abort(404, f"No gem named “{name}” was found on rubygems.org.")
    except UpstreamUnavailable:
        abort(
            502,
            "rubygems.org could not be reached and this gem has not been cached here yet. "
            "Try again in a moment.",
        )
