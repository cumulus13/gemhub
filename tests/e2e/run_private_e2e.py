"""End-to-end check of private gems with the real ``gem`` and ``bundle`` clients.

Usage: python tests/e2e/run_private_e2e.py <fixture dir from build_fixture.sh>
Pushes a private gem that depends on a public one, installs it with ``gem`` and Bundler,
publishes a second version, updates, then yanks it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_ruby_e2e import BUNDLE  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from fake_upstream import FakeUpstream  # noqa: E402
from gemhub.app import create_app  # noqa: E402
from gemhub.auth import create_key  # noqa: E402
from gemhub.config import Settings  # noqa: E402

GEMSPEC = """Gem::Specification.new do |s|
  s.name = "acme_billing"
  s.version = "%(version)s"
  s.summary = "Internal billing client"
  s.authors = ["Edi"]
  s.files = ["lib/acme_billing.rb"]
  s.license = "MIT"
  s.add_runtime_dependency "hellohub", ">= 0.3"
end
"""


def main(fixture: Path) -> int:
    up = FakeUpstream()
    up.deps = ""
    up.add_gem("hellohub", "0.3.1", content=(fixture / "hellohub-0.3.1.gem").read_bytes())
    from flask import Response

    @up.app.get("/quick/Marshal.4.8/<filename>")
    def quick(filename):
        return Response((fixture / filename).read_bytes(), mimetype="application/octet-stream")

    for name in ("specs", "latest_specs", "prerelease_specs"):
        up.specs_files[name] = (fixture / f"{name}.4.8.gz").read_bytes()
    up.start()

    work = Path(tempfile.mkdtemp(prefix="gemhub-priv-"))
    settings = Settings.from_env(
        {
            "GEMHUB_DATA_DIR": str(work / "data"),
            "GEMHUB_UPSTREAM_URL": up.url,
            "GEMHUB_UPSTREAM_RETRIES": "0",
            "GEMHUB_LOG_LEVEL": "WARNING",
            "GEMHUB_VERSIONS_TTL": "0",
            "GEMHUB_INFO_TTL": "0",
        }
    )
    app = create_app(settings)
    with app.app_context():
        secret = create_key("e2e")[1]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hub = f"http://127.0.0.1:{server.server_port}"
    env = {
        **os.environ,
        "GEM_HOME": str(work / "gems"),
        "GEM_PATH": str(work / "gems"),
        "HOME": str(work),
        "GEM_HOST_API_KEY": secret,
    }
    failures = 0

    def run(title: str, cmd: list, cwd: Path = work, expect_ok: bool = True) -> str:
        nonlocal failures
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        ok = (proc.returncode == 0) == expect_ok
        print(("PASS " if ok else "FAIL ") + title)
        if not ok:
            failures += 1
            print(proc.stdout[-1800:], proc.stderr[-1800:])
        return proc.stdout + proc.stderr

    def build(version: str) -> Path:
        src = work / f"src-{version}"
        (src / "lib").mkdir(parents=True, exist_ok=True)
        (src / "lib" / "acme_billing.rb").write_text(
            f'module AcmeBilling; VERSION = "{version}"; end\n'
        )
        (src / "acme_billing.gemspec").write_text(GEMSPEC % {"version": version})
        subprocess.run(
            ["gem", "build", "acme_billing.gemspec"],
            cwd=src,
            env=env,
            capture_output=True,
            check=True,
        )
        return src / f"acme_billing-{version}.gem"

    run("gem push 0.1.0", ["gem", "push", "--host", hub, str(build("0.1.0"))])
    run(
        "gem push of the same version is refused",
        ["gem", "push", "--host", hub, str(build("0.1.0"))],
        expect_ok=False,
    )
    run(
        "gem install private gem (pulls public dependency)",
        ["gem", "install", "acme_billing", "--clear-sources", "--source", hub, "--no-document"],
    )
    listing = run("gem list", ["gem", "list"])
    if "acme_billing (0.1.0)" not in listing or "hellohub (0.3.1)" not in listing:
        failures += 1
        print("FAIL both gems should be installed:", listing)

    project = work / "project"
    project.mkdir()
    (project / "Gemfile").write_text(f'source "{hub}"\ngem "acme_billing"\n')
    run("bundle install resolves private + public", BUNDLE + ["install"], cwd=project)
    out = run(
        "bundle exec loads both",
        BUNDLE
        + [
            "exec",
            "ruby",
            "-e",
            "require 'acme_billing'; require 'hellohub'; puts AcmeBilling::VERSION",
        ],
        cwd=project,
    )
    if "0.1.0" not in out:
        failures += 1

    run("gem push 0.2.0", ["gem", "push", "--host", hub, str(build("0.2.0"))])
    run(
        "bundle update picks up the new private version",
        BUNDLE + ["update", "acme_billing"],
        cwd=project,
    )
    out = run(
        "bundle exec shows 0.2.0",
        BUNDLE + ["exec", "ruby", "-e", "require 'acme_billing'; puts AcmeBilling::VERSION"],
        cwd=project,
    )
    if "0.2.0" not in out:
        failures += 1
        print("FAIL expected 0.2.0 after update:", out)

    run("gem yank 0.2.0", ["gem", "yank", "acme_billing", "-v", "0.2.0", "--host", hub])
    info = subprocess.run(
        ["curl", "-fsS", f"{hub}/info/acme_billing"], capture_output=True, text=True
    ).stdout
    if "0.2.0" in info or "0.1.0" not in info:
        failures += 1
        print("FAIL yank should hide 0.2.0 from the index:", info)
    else:
        print("PASS yanked version is gone from /info")

    server.shutdown()
    up.stop()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main(Path(sys.argv[1])) else 0)
