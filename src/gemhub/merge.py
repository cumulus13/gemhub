"""Compact index files that combine the upstream mirror with this hub's private gems."""

from __future__ import annotations

import base64
import contextlib
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
            current = self.root / meta["file"] if meta.get("file") else None
            if meta.get("key") != key or current is None or not current.exists():
                # Always a new file: Windows cannot replace a file another request is reading.
                new = self.root / f"{name}.{uuid.uuid4().hex[:16]}.body"
                writer(new, base.path if base and base.status == 200 else None)
                md5, sha = hashlib.md5(usedforsecurity=False), hashlib.sha256()
                with open(new, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        md5.update(chunk)
                        sha.update(chunk)
                meta = {
                    "key": key,
                    "file": new.name,
                    "status": 200,
                    "fetched_at": time.time(),
                    "etag": f'"{md5.hexdigest()}"',
                    "repr-digest": "sha-256=:" + base64.b64encode(sha.digest()).decode() + ":",
                    "content-type": TEXT,
                }
                tmp = meta_path.with_name(f"{meta_path.name}.{uuid.uuid4().hex}.tmp")
                tmp.write_text(json.dumps(meta), "utf-8")
                replace_file(tmp, meta_path)
                for old in self.root.glob(f"{name}.*.body"):
                    if old.name != new.name:
                        with contextlib.suppress(OSError):
                            old.unlink()
                current = new
        return Entry(200, current, meta, stale=bool(base and base.stale))

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
            # Bytes, not text mode: Windows would turn every "\n" into "\r\n" and corrupt the index.
            tmp.write_bytes(("---\n" + "".join(f"{n}\n" for n in sorted(names))).encode("utf-8"))

        return self._build("names", base, token, write)
