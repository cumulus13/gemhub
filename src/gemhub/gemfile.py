"""Read the metadata of an uploaded ``.gem`` safely.

A ``.gem`` is a tar archive holding ``metadata.gz`` (the gemspec as YAML), ``data.tar.gz``
and, usually, ``checksums.yaml.gz``. Nothing is extracted to disk and the YAML is parsed
with a loader that never instantiates objects.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .names import valid_gem_name

MAX_METADATA_BYTES = 2 * 1024 * 1024
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9A-Za-z]+)*$")
PLATFORM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class InvalidGem(ValueError):
    """The upload is not a valid, acceptable gem."""


class _Loader(yaml.SafeLoader):
    """YAML loader that turns ``!ruby/...`` tagged nodes into plain dicts, lists, scalars."""


def _ruby_tag(loader: _Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_Loader.add_multi_constructor("!ruby/", _ruby_tag)


class _TextLoader(yaml.SafeLoader):
    """Every scalar stays a string, so a hex digest such as ``0000...`` is never read as 0."""


_TextLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag == "tag:yaml.org,2002:null"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass
class ParsedGem:
    name: str
    version: str
    platform: str
    summary: str
    description: Optional[str]
    authors: List[str]
    email: Any
    homepage: Optional[str]
    licenses: List[str]
    metadata: Dict[str, str]
    required_ruby: List[Tuple[str, str]]
    required_rubygems: List[Tuple[str, str]]
    dependencies: List[Dict[str, Any]]  # name, kind, requirements [(op, version)]
    rubygems_version: str
    date: datetime
    sha256: str
    size: int
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        suffix = "" if self.platform == "ruby" else f"-{self.platform}"
        return f"{self.name}-{self.version}{suffix}.gem"


def _read_member(archive: tarfile.TarFile, name: str, limit: Optional[int]) -> Optional[bytes]:
    try:
        member = archive.getmember(name)
    except KeyError:
        return None
    if not member.isfile():
        raise InvalidGem(f"{name} is not a regular file")
    handle = archive.extractfile(member)
    if handle is None:
        return None
    data = handle.read() if limit is None else handle.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise InvalidGem(f"{name} is too large")
    return data


def _requirement(node: Any) -> List[Tuple[str, str]]:
    """``!ruby/object:Gem::Requirement`` as ``[(operator, version), ...]``."""
    if not isinstance(node, dict):
        return [(">=", "0")]
    parts: List[Tuple[str, str]] = []
    for item in node.get("requirements") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise InvalidGem("malformed requirement in gem metadata")
        op, version = item
        text = version.get("version") if isinstance(version, dict) else version
        parts.append((str(op), str(text)))
    return parts or [(">=", "0")]


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _check_checksums(archive: tarfile.TarFile, members: Dict[str, bytes]) -> None:
    raw = _read_member(archive, "checksums.yaml.gz", MAX_METADATA_BYTES)
    if raw is None:
        return
    try:
        text = gzip.decompress(raw)
        sums = yaml.load(text, Loader=_TextLoader)  # noqa: S506 - a SafeLoader
    except (OSError, EOFError, yaml.YAMLError) as exc:
        raise InvalidGem("checksums.yaml.gz is unreadable") from exc
    expected = (sums or {}).get("SHA256") if isinstance(sums, dict) else None
    if not isinstance(expected, dict):
        return
    for name, data in members.items():
        want = expected.get(name)
        if want is not None and hashlib.sha256(data).hexdigest() != str(want).strip().lower():
            raise InvalidGem(f"checksum of {name} does not match checksums.yaml.gz")


def parse_gem(data: bytes) -> ParsedGem:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")  # noqa: SIM115
    except tarfile.TarError as exc:
        raise InvalidGem("not a gem file (invalid tar archive)") from exc
    with archive:
        metadata_gz = _read_member(archive, "metadata.gz", MAX_METADATA_BYTES)
        data_tar = _read_member(archive, "data.tar.gz", None)
        if metadata_gz is None or data_tar is None:
            raise InvalidGem("gem must contain metadata.gz and data.tar.gz")
        _check_checksums(archive, {"metadata.gz": metadata_gz, "data.tar.gz": data_tar})
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(metadata_gz)) as gz:
            raw = gz.read(MAX_METADATA_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise InvalidGem("metadata.gz is not valid gzip") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise InvalidGem("gem metadata is too large")
    try:
        spec = yaml.load(raw, Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    except yaml.YAMLError as exc:
        raise InvalidGem("gem metadata is not valid YAML") from exc
    if not isinstance(spec, dict):
        raise InvalidGem("gem metadata has an unexpected shape")

    name = spec.get("name")
    if not isinstance(name, str) or not valid_gem_name(name):
        raise InvalidGem("invalid gem name")
    version_node = spec.get("version")
    version = version_node.get("version") if isinstance(version_node, dict) else None
    version = str(version) if version is not None else ""
    if not VERSION_RE.match(version) or len(version) > 128:
        raise InvalidGem(f"invalid gem version {version!r}")
    platform = spec.get("platform") or "ruby"
    if not isinstance(platform, str) or not PLATFORM_RE.match(platform):
        raise InvalidGem("invalid gem platform")

    dependencies: List[Dict[str, Any]] = []
    for dep in spec.get("dependencies") or []:
        if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
            raise InvalidGem("malformed dependency in gem metadata")
        kind = str(dep.get("type") or ":runtime").lstrip(":")
        if kind not in ("runtime", "development"):
            raise InvalidGem(f"unknown dependency type {kind!r}")
        dependencies.append(
            {
                "name": dep["name"],
                "kind": kind,
                "requirements": _requirement(dep.get("requirement")),
            }
        )

    metadata = spec.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise InvalidGem("metadata must be a mapping")

    return ParsedGem(
        name=name,
        version=version,
        platform=platform,
        summary=str(spec.get("summary") or ""),
        description=str(spec["description"]) if spec.get("description") else None,
        authors=_str_list(spec.get("authors")),
        email=spec.get("email"),
        homepage=str(spec["homepage"]) if spec.get("homepage") else None,
        licenses=_str_list(spec.get("licenses")),
        metadata={str(k): str(v) for k, v in metadata.items()},
        required_ruby=_requirement(spec.get("required_ruby_version")),
        required_rubygems=_requirement(spec.get("required_rubygems_version")),
        dependencies=dependencies,
        rubygems_version=str(spec.get("rubygems_version") or "3.4.20"),
        date=_as_datetime(spec.get("date")),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
