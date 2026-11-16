"""Container for the long-lived services attached to the Flask app."""

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app

from .cache import Cache
from .config import Settings
from .gems import GemStore
from .merge import MergedIndex
from .metadata import MetadataService
from .private import PrivateService
from .proxy import ProxyCache
from .upstream import Upstream


@dataclass
class Hub:
    settings: Settings
    upstream: Upstream
    cache: Cache
    proxy: ProxyCache
    gems: GemStore
    metadata: MetadataService
    private: PrivateService
    merged: MergedIndex


def get_hub() -> Hub:
    return current_app.extensions["gemhub"]
