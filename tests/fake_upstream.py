"""A small in-process imitation of rubygems.org used by the tests."""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from typing import Any, Dict, List

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class FakeUpstream:
    def __init__(self) -> None:
        self.hits: Counter = Counter()
        self.range_headers: List[str] = []
        self.fail = False
        self.corrupt_gems = False
        self.gems: Dict[str, Dict[str, Any]] = {}
        self.versions_lines: List[str] = []
        self.versions_override: bytes = b""
        self.specs_files: Dict[str, bytes] = {}
        self.deps = "rack:>= 2.0"
        self.app = Flask("fake-upstream")
        self._routes()
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> FakeUpstream:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)

    # -- data builders -----------------------------------------------------

    def add_gem(
        self,
        name: str,
        version: str = "1.0.0",
        *,
        content: bytes = b"",
        downloads: int = 1234,
        info: str = "A test gem",
        **extra: Any,
    ) -> bytes:
        content = content or (f"gem:{name}:{version}".encode() * 50)
        record = self.gems.setdefault(name, {"versions": [], "meta": {}})
        record["versions"].insert(0, {"number": version, "content": content})
        record["meta"] = {
            "name": name,
            "version": version,
            "downloads": downloads,
            "version_downloads": downloads // 2,
            "platform": "ruby",
            "authors": "Ada Lovelace, Grace Hopper",
            "info": info,
            "licenses": ["MIT"],
            "sha": hashlib.sha256(content).hexdigest(),
            "project_uri": f"https://rubygems.org/gems/{name}",
            "homepage_uri": f"https://example.org/{name}",
            "dependencies": {
                "development": [{"name": "rake", "requirements": ">= 0"}],
                "runtime": [{"name": "rack", "requirements": ">= 2.0"}],
            },
            **extra,
        }
        self.versions_lines.append(f"{name} {version} {_md5(self._info(name).encode())}")
        return content

    def _info(self, name: str) -> str:
        lines = ["---"]
        for v in reversed(self.gems[name]["versions"]):
            checksum = hashlib.sha256(v["content"]).hexdigest()
            lines.append(f"{v['number']} {self.deps}|checksum:{checksum},ruby:>= 2.5.0")
        return "\n".join(lines) + "\n"

    def versions_body(self) -> bytes:
        if self.versions_override:
            return self.versions_override
        return (
            "created_at: 2026-01-01T00:00:00Z\n---\n" + "\n".join(self.versions_lines) + "\n"
        ).encode()

    # -- routes ------------------------------------------------------------

    def _guard(self):
        self.hits[request.path] += 1
        if self.fail:
            return Response("down", 503)
        return None

    def _body(self, body: bytes, mimetype: str = "text/plain; charset=utf-8") -> Response:
        etag = f'"{_md5(body)}"'
        if request.headers.get("If-None-Match") == etag:
            resp = Response(status=304)
            resp.headers["ETag"] = etag
            return resp
        header = request.headers.get("Range")
        if header:
            self.range_headers.append(header)
            match = re.fullmatch(r"bytes=(\d+)-", header)
            if match and int(match.group(1)) < len(body):
                start = int(match.group(1))
                resp = Response(body[start:], 206, mimetype=mimetype)
                resp.headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
                resp.headers["ETag"] = etag
                return resp
            if match:
                resp = Response(status=416)
                resp.headers["Content-Range"] = f"bytes */{len(body)}"
                return resp
        resp = Response(body, 200, mimetype=mimetype)
        resp.headers["ETag"] = etag
        return resp

    def _routes(self) -> None:
        app = self.app

        @app.get("/versions")
        def versions():
            return self._guard() or self._body(self.versions_body())

        @app.get("/names")
        def names():
            return self._guard() or self._body(("---\n" + "\n".join(self.gems) + "\n").encode())

        @app.get("/info/<name>")
        def info(name):
            if (bad := self._guard()) is not None:
                return bad
            if name not in self.gems:
                return Response("not found", 404)
            return self._body(self._info(name).encode())

        @app.get("/gems/<filename>")
        def gem_file(filename):
            if (bad := self._guard()) is not None:
                return bad
            for name, record in self.gems.items():
                for v in record["versions"]:
                    if filename == f"{name}-{v['number']}.gem":
                        data = v["content"]
                        if self.corrupt_gems:
                            data = data[:-1] + b"X"
                        return Response(data, 200, mimetype="application/octet-stream")
            return Response("not found", 404)

        @app.get("/api/v1/gems/<name>.json")
        def api_gem(name):
            if (bad := self._guard()) is not None:
                return bad
            if name not in self.gems:
                return Response("not found", 404)
            return jsonify(self.gems[name]["meta"])

        @app.get("/api/v1/versions/<name>.json")
        def api_versions(name):
            if (bad := self._guard()) is not None:
                return bad
            if name not in self.gems:
                return Response("not found", 404)
            return jsonify(
                [
                    {
                        "number": v["number"],
                        "platform": "ruby",
                        "created_at": "2025-03-04T10:00:00.000Z",
                        "downloads_count": 100 * (i + 1),
                        "prerelease": False,
                        "ruby_version": ">= 2.5.0",
                        "sha": hashlib.sha256(v["content"]).hexdigest(),
                        "licenses": ["MIT"],
                        "summary": "dropped by the hub",
                    }
                    for i, v in enumerate(self.gems[name]["versions"])
                ]
            )

        @app.get("/api/v1/gems/<name>/owners.json")
        def api_owners(name):
            if (bad := self._guard()) is not None:
                return bad
            if name not in self.gems:
                return Response("not found", 404)
            return jsonify([{"id": 1, "handle": "ada"}])

        @app.get("/api/v1/search.json")
        def api_search():
            if (bad := self._guard()) is not None:
                return bad
            query = request.args.get("query", "")
            return jsonify([r["meta"] for n, r in self.gems.items() if query.lower() in n.lower()])

        @app.get("/<name>.4.8.gz")
        def specs(name):
            if (bad := self._guard()) is not None:
                return bad
            data = self.specs_files.get(name, b"\x1f\x8bfake" if name == "specs" else None)
            if data is None:
                return Response("not found", 404)
            return self._body(data, "application/octet-stream")
