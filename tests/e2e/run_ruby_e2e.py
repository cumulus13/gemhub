"""End-to-end check with the real ``gem`` and ``bundle`` clients against gemhub.

Requires Ruby. Usage: python tests/e2e/run_ruby_e2e.py <dir containing a built fixture gem>
The fixture directory must hold hellohub-0.3.1.gem, hellohub-0.3.1.gemspec.rz and the
three *specs.4.8.gz files (see README "Development").
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Response  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from fake_upstream import FakeUpstream  # noqa: E402
from gemhub.app import create_app  # noqa: E402
from gemhub.config import Settings  # noqa: E402

BUNDLE = [
    "ruby",
    "-e",
    "require 'bundler'; require 'bundler/friendly_errors'; require 'bundler/cli'; "
    "Bundler::CLI.start(ARGV, debug: true)",
]


def main(fixture: Path) -> int:
    up = FakeUpstream()
    up.deps = ""
    gem_bytes = (fixture / "hellohub-0.3.1.gem").read_bytes()
    up.add_gem("hellohub", "0.3.1", content=gem_bytes)

    @up.app.get("/quick/Marshal.4.8/<filename>")
    def quick(filename):
        up.hits["/quick"] += 1
        return Response((fixture / filename).read_bytes(), mimetype="application/octet-stream")

    for name in ("specs", "latest_specs", "prerelease_specs"):
        up.specs_files[name] = (fixture / f"{name}.4.8.gz").read_bytes()
    up.start()

    work = Path(tempfile.mkdtemp(prefix="gemhub-e2e-"))
    settings = Settings.from_env(
        {
            "GEMHUB_DATA_DIR": str(work / "data"),
            "GEMHUB_UPSTREAM_URL": up.url,
            "GEMHUB_UPSTREAM_RETRIES": "0",
            "GEMHUB_LOG_LEVEL": "WARNING",
        }
    )
    server = make_server("127.0.0.1", 0, create_app(settings), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hub = f"http://127.0.0.1:{server.server_port}"
    env = {
        **os.environ,
        "GEM_HOME": str(work / "gems"),
        "GEM_PATH": str(work / "gems"),
        "HOME": str(work),
    }
    failures = 0

    def run(title: str, cmd: list, cwd: Path = work) -> bool:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        ok = proc.returncode == 0
        print(("PASS " if ok else "FAIL ") + title)
        if not ok:
            print(proc.stdout[-1500:], proc.stderr[-1500:])
        return ok

    if not run(
        "gem install via hub",
        ["gem", "install", "hellohub", "--clear-sources", "--source", hub, "--no-document"],
    ):
        failures += 1
    ls = subprocess.run(["gem", "list", "hellohub"], env=env, capture_output=True, text=True)
    print("gem list ->", ls.stdout.strip())
    if "0.3.1" not in ls.stdout:
        failures += 1

    project = work / "project"
    project.mkdir()
    (project / "Gemfile").write_text(f'source "{hub}"\ngem "hellohub", "~> 0.3"\n')
    if not run("bundle install via hub", BUNDLE + ["install"], cwd=project):
        failures += 1
    if not run(
        "bundle exec require",
        BUNDLE + ["exec", "ruby", "-e", "require 'hellohub'; puts Hellohub::VERSION"],
        cwd=project,
    ):
        failures += 1

    print("upstream hits:", {k: v for k, v in up.hits.items() if k.startswith(("/gems", "/quick"))})
    server.shutdown()
    up.stop()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main(Path(sys.argv[1])) else 0)
