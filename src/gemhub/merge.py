"""Compact index files that combine the upstream mirror with this hub's private gems."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import Settings
from .proxy import Entry, replace_file

TEXT = "text/plain; charset=utf-8"


class MergedIndex:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.data_dir / "proxy" / "merged"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _build(
        self,
        name: str,
        base: Optional[Entry],
        token: str,
        writer: Callable[[Path, Optional[Path]], None],
    ) -> Entry:
        body = self.root / name
        meta_path = self.root / f"{name}.json"
        base_tag = (
            (base.meta.get("etag") or str(base.meta.get("fetched_at"))) if base else "offline"
        )
        key = f"{base_tag}|{token}"
        with self._lock:
            try:
                meta: Dict = json.loads(meta_path.read_text("utf-8"))
            except (OSError, ValueError):
                meta = {}
            if meta.get("key") != key or not body.exists():
                tmp = body.with_name(f"{name}.{uuid.uuid4().hex}.tmp")
                writer(tmp, base.path if base and base.status == 200 else None)
                md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
                with open(tmp, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        md5.update(chunk)
                        sha.update(chunk)
                replace_file(tmp, body)
                meta = {
                    "key": key,
                    "status": 200,
                    "fetched_at": time.time(),
                    "etag": f'"{md5.hexdigest()}"',
                    "repr-digest": "sha-256=:" + base64.b64encode(sha.digest()).decode() + ":",
                    "content-type": TEXT,
                }
                meta_path.write_text(json.dumps(meta), "utf-8")
        return Entry(200, body, meta, stale=bool(base and base.stale))

    def versions(self, base: Optional[Entry], extra: bytes, token: str) -> Entry:
        def write(tmp: Path, source: Optional[Path]) -> None:
            with open(tmp, "wb") as out:
                if source is None:
                    out.write(b"---\n")
                else:
                    with open(source, "rb") as src:
                        last = b"\n"
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            out.write(chunk)
                            last = chunk[-1:]
                    if last != b"\n":
                        out.write(b"\n")
                out.write(extra)

        return self._build("versions", base, token, write)

    def names(self, base: Optional[Entry], private: List[str], token: str) -> Entry:
        def write(tmp: Path, source: Optional[Path]) -> None:
            names = set(private)
            if source is not None:
                lines = source.read_text("utf-8", errors="replace").splitlines()
                after = False
                for line in lines:
                    if after and line.strip():
                        names.add(line.strip())
                    elif line.strip() == "---":
                        after = True
            tmp.write_text("---\n" + "".join(f"{n}\n" for n in sorted(names)), "utf-8")

        return self._build("names", base, token, write)
