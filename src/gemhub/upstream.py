"""HTTP client for the upstream RubyGems server."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Upstream could not be reached, or answered with an unusable status."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class UpstreamNotFound(UpstreamError):
    """Upstream answered 404 or 410."""


class Upstream:
    def __init__(self, settings: Settings) -> None:
        self.base = settings.upstream_url
        self._timeout = (settings.connect_timeout, settings.read_timeout)
        retry = Retry(
            total=settings.retries,
            connect=settings.retries,
            read=settings.retries,
            status=settings.retries,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self._session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=32)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._session.headers.update({"User-Agent": settings.user_agent})

    def get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
    ) -> requests.Response:
        """GET ``path`` and return the response whatever its status; callers decide.

        Only transport failures raise ``UpstreamError``.
        """
        url = self.base + path
        try:
            return self._session.get(
                url,
                params=params,
                headers=headers,
                stream=stream,
                timeout=self._timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise UpstreamError(f"GET {url} failed: {exc}") from exc

    def get_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self.get(path, params=params, headers={"Accept": "application/json"})
        try:
            if resp.status_code in (404, 410):
                raise UpstreamNotFound(f"{path} not found", resp.status_code)
            if resp.status_code != 200:
                raise UpstreamError(f"{path} answered HTTP {resp.status_code}", resp.status_code)
            try:
                return resp.json()
            except ValueError as exc:
                raise UpstreamError(f"{path} returned invalid JSON") from exc
        finally:
            resp.close()
