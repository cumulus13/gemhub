"""Template helpers: formatting filters, pagination and small SVG generators."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import urlsplit

from .models import utcnow


def intcomma(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def ago(value: Optional[datetime]) -> str:
    if value is None:
        return "never"
    seconds = int((utcnow() - value).total_seconds())
    if seconds < 45:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            amount = max(1, round(seconds / size))
            return f"{amount} {unit}{'' if amount == 1 else 's'} ago"
    return "just now"


def datefmt(value: Any) -> str:
    """Render an ISO 8601 timestamp as ``Mar 4, 2025``; fall back to the raw text."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def filesize(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def safe_url(value: Any) -> str:
    """Only http(s) URLs survive; upstream metadata is untrusted (``javascript:`` etc.)."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    parts = urlsplit(candidate)
    if parts.scheme in ("http", "https") and parts.netloc:
        return candidate
    return ""


def host_of(value: Any) -> str:
    return urlsplit(value).netloc if isinstance(value, str) else ""


_NUMERIC_VERSION = re.compile(r"^\d+(\.\d+)*$")


def pessimistic(version: str) -> str:
    """Gemfile requirement for ``version``: ``~> 7.1`` for releases, exact for pre-releases."""
    if not _NUMERIC_VERSION.match(version or ""):
        return version or ""
    parts = version.split(".")
    return "~> " + ".".join(parts[:2]) if len(parts) > 1 else "~> " + parts[0]


def percent(value: Any, maximum: Any) -> float:
    try:
        v, m = float(value), float(maximum)
    except (TypeError, ValueError):
        return 0.0
    if m <= 0:
        return 0.0
    return round(max(0.0, min(100.0, v / m * 100.0)), 2)


def sparkline(
    values: Sequence[int], width: int = 240, height: int = 44, pad: int = 3
) -> Dict[str, Any]:
    count = len(values)
    peak = max(values) if values else 0
    inner_h = height - 2 * pad
    points: List[str] = []
    for index, value in enumerate(values):
        x = pad + (width - 2 * pad) * (index / (count - 1) if count > 1 else 0)
        y = height - pad - (inner_h * (value / peak) if peak else 0)
        points.append(f"{x:.1f},{y:.1f}")
    line = " ".join(points)
    area = f"{pad},{height - pad} {line} {width - pad},{height - pad}" if points else ""
    return {"line": line, "area": area, "peak": peak, "width": width, "height": height}


@dataclass(frozen=True)
class Pagination:
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_item(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.per_page + 1

    @property
    def last_item(self) -> int:
        return min(self.total, self.page * self.per_page)

    def window(self, around: int = 2) -> Iterator[Optional[int]]:
        """Page numbers to show; ``None`` marks a gap."""
        shown = sorted(
            {1, self.pages}
            | {p for p in range(self.page - around, self.page + around + 1) if 1 <= p <= self.pages}
        )
        previous = 0
        for number in shown:
            if number - previous > 1:
                yield None
            yield number
            previous = number


def register(app) -> None:
    app.jinja_env.filters.update(
        intcomma=intcomma,
        ago=ago,
        datefmt=datefmt,
        filesize=filesize,
        safe_url=safe_url,
        host_of=host_of,
        pessimistic=pessimistic,
        percent=percent,
    )
    app.jinja_env.globals["sparkline"] = sparkline
