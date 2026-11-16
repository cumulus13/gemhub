"""Small JSON cache: Redis when configured, an in-process TTL cache otherwise."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional, Tuple

import redis

log = logging.getLogger(__name__)


class _MemoryTTL:
    def __init__(self, max_entries: int = 10000) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, Tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            if item[0] <= now:
                del self._data[key]
                return None
            return item[1]

    def set(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class Cache:
    """JSON values with a TTL. Redis failures degrade to the in-process cache, never to errors."""

    def __init__(self, redis_url: Optional[str], prefix: str = "gemhub:") -> None:
        self.prefix = prefix
        self._memory = _MemoryTTL()
        self._redis: Optional[redis.Redis] = None
        self._redis_down_logged = False
        if redis_url:
            self._redis = redis.Redis.from_url(
                redis_url,
                socket_timeout=2,
                socket_connect_timeout=2,
                health_check_interval=30,
                decode_responses=True,
            )

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def _redis_failed(self, exc: Exception) -> None:
        if not self._redis_down_logged:
            log.warning("Redis unavailable, using in-process cache until it recovers: %s", exc)
            self._redis_down_logged = True

    def _redis_ok(self) -> None:
        if self._redis_down_logged:
            log.info("Redis is reachable again")
            self._redis_down_logged = False

    def get_json(self, key: str) -> Any:
        full = self.prefix + key
        raw: Optional[str] = None
        if self._redis is not None:
            try:
                raw = self._redis.get(full)
                self._redis_ok()
            except redis.RedisError as exc:
                self._redis_failed(exc)
                raw = self._memory.get(full)
        else:
            raw = self._memory.get(full)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        full = self.prefix + key
        raw = json.dumps(value, separators=(",", ":"), default=str)
        if self._redis is not None:
            try:
                self._redis.set(full, raw, ex=ttl)
                self._redis_ok()
                return
            except redis.RedisError as exc:
                self._redis_failed(exc)
        self._memory.set(full, raw, ttl)

    def delete(self, key: str) -> None:
        full = self.prefix + key
        self._memory.delete(full)
        if self._redis is not None:
            try:
                self._redis.delete(full)
            except redis.RedisError as exc:
                self._redis_failed(exc)

    def ping(self) -> Optional[bool]:
        """``None`` when Redis is not configured, otherwise whether it answers."""
        if self._redis is None:
            return None
        try:
            return bool(self._redis.ping())
        except redis.RedisError:
            return False
