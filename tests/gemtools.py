"""Build real ``.gem`` archives in pure Python for tests (no Ruby needed)."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from typing import Sequence, Tuple


def _dependency(name: str, op: str, version: str, kind: str) -> str:
    req = (
        "    requirements:\n"
        f'    - - "{op}"\n'
        "      - !ruby/object:Gem::Version\n"
        f"        version: '{version}'\n"
    )
    return (
        "- !ruby/object:Gem::Dependency\n"
        f"  name: {name}\n"
        "  requirement: !ruby/object:Gem::Requirement\n"
        f"{req}"
        f"  type: :{kind}\n"
        "  prerelease: false\n"
        "  version_requirements: !ruby/object:Gem::Requirement\n"
        f"{req}"
    )


def _gz(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


def build_gem(
    name: str,
    version: str = "1.0.0",
    *,
    deps: Sequence[Tuple[str, str, str, str]] = (),
    summary: str = "A private test gem",
    description: str = "Longer description of the private gem",
    platform: str = "ruby",
    required_ruby: str = "2.5.0",
    break_checksums: bool = False,
    include_data: bool = True,
) -> bytes:
    deps_yaml = "".join(_dependency(*d) for d in deps) if deps else ""
    deps_block = f"dependencies:\n{deps_yaml}" if deps else "dependencies: []\n"
    metadata = (
        "--- !ruby/object:Gem::Specification\n"
        f"name: {name}\n"
        "version: !ruby/object:Gem::Version\n"
        f"  version: {version}\n"
        f"platform: {platform}\n"
        "authors:\n- Ada Lovelace\n"
        "autorequire:\nbindir: bin\ncert_chain: []\n"
        "date: 2026-11-16 00:00:00.000000000 Z\n"
        f"{deps_block}"
        f"description: {description}\n"
        "email: ada@example.org\nexecutables: []\nextensions: []\nextra_rdoc_files: []\n"
        f"files:\n- lib/{name}.rb\n"
        "homepage: https://example.org/private\n"
        "licenses:\n- MIT\n"
        "metadata:\n  source_code_uri: https://example.org/src\n"
        "post_install_message:\nrdoc_options: []\nrequire_paths:\n- lib\n"
        "required_ruby_version: !ruby/object:Gem::Requirement\n"
        "  requirements:\n"
        f'  - - ">="\n    - !ruby/object:Gem::Version\n      version: {required_ruby}\n'
        "required_rubygems_version: !ruby/object:Gem::Requirement\n"
        "  requirements:\n"
        "  - - \">=\"\n    - !ruby/object:Gem::Version\n      version: '0'\n"
        "requirements: []\nrubygems_version: 3.4.20\nsigning_key:\n"
        "specification_version: 4\n"
        f"summary: {summary}\ntest_files: []\n"
    )
    metadata_gz = _gz(metadata.encode())

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as inner:
        body = f"module X; end # {name} {version}\n".encode()
        info = tarfile.TarInfo(f"lib/{name}.rb")
        info.size = len(body)
        inner.addfile(info, io.BytesIO(body))
    data_tar_gz = _gz(payload.getvalue())

    sums = {
        "metadata.gz": hashlib.sha256(metadata_gz).hexdigest(),
        "data.tar.gz": hashlib.sha256(data_tar_gz).hexdigest(),
    }
    if break_checksums:
        sums["data.tar.gz"] = "0" * 64
    checksums = "---\nSHA256:\n" + "".join(f"  {k}: {v}\n" for k, v in sums.items())
    checksums_gz = _gz(checksums.encode())

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as outer:
        members = [("metadata.gz", metadata_gz)]
        if include_data:
            members.append(("data.tar.gz", data_tar_gz))
        members.append(("checksums.yaml.gz", checksums_gz))
        for member_name, content in members:
            tar_info = tarfile.TarInfo(member_name)
            tar_info.size = len(content)
            outer.addfile(tar_info, io.BytesIO(content))
    return out.getvalue()
