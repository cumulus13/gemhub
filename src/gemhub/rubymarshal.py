"""A minimal Ruby ``Marshal`` (format 4.8) writer, enough to emit ``Gem::Specification``.

RubyGems clients download ``/quick/Marshal.4.8/<name>-<version>.gemspec.rz`` (a zlib
stream of this format) to learn a gem's dependencies before installing it. Upstream
rubygems.org serves those for public gems; for private gems the hub has to produce them.
The layout mirrors ``Gem::Specification#_dump`` and was cross-checked against the bytes
produced by real Ruby (see tests/e2e).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

SPECIFICATION_VERSION = 4


class Sym(str):
    """A Ruby Symbol."""


@dataclass(frozen=True)
class RVersion:
    text: str


@dataclass(frozen=True)
class RRequirement:
    """``Gem::Requirement``: a list of ``(operator, version)`` pairs."""

    parts: Tuple[Tuple[str, str], ...] = ((">=", "0"),)


@dataclass(frozen=True)
class RDependency:
    name: str
    requirement: RRequirement
    kind: str = "runtime"  # "runtime" or "development"


@dataclass(frozen=True)
class RTime:
    value: datetime


@dataclass
class SpecData:
    """The fields of a gem specification that clients read from the quick index."""

    name: str
    version: str
    summary: str = ""
    description: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    email: Any = None  # None, a string or a list of strings
    homepage: Optional[str] = None
    licenses: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)
    required_ruby_version: RRequirement = RRequirement()
    required_rubygems_version: RRequirement = RRequirement()
    dependencies: List[RDependency] = field(default_factory=list)
    rubygems_version: str = "3.4.20"
    date: Optional[datetime] = None


def _long(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    if 0 < n < 123:
        return bytes([n + 5])
    if -124 < n < 0:
        return bytes([(n - 5) & 0xFF])
    out = bytearray()
    x = n
    for count in range(1, 5):
        out.append(x & 0xFF)
        x >>= 8
        if x == 0:
            return bytes([count]) + bytes(out)
        if x == -1:
            return bytes([(-count) & 0xFF]) + bytes(out)
    raise ValueError("integer does not fit in a Marshal long")


class _Writer:
    def __init__(self) -> None:
        self.out = bytearray(b"\x04\x08")
        self._symbols: Dict[str, int] = {}

    def symbol(self, name: str) -> None:
        index = self._symbols.get(name)
        if index is not None:
            self.out += b";" + _long(index)
            return
        self._symbols[name] = len(self._symbols)
        raw = name.encode("utf-8")
        self.out += b":" + _long(len(raw)) + raw

    def string(self, value: str) -> None:
        raw = value.encode("utf-8")
        self.out += b'I"' + _long(len(raw)) + raw + _long(1)
        self.symbol("E")
        self.out += b"T"

    def value(self, v: Any) -> None:
        if v is None:
            self.out += b"0"
        elif v is True:
            self.out += b"T"
        elif v is False:
            self.out += b"F"
        elif isinstance(v, Sym):
            self.symbol(str(v))
        elif isinstance(v, int):
            if not -(2**30) <= v < 2**30:
                raise ValueError("only 31-bit integers are supported")
            self.out += b"i" + _long(v)
        elif isinstance(v, str):
            self.string(v)
        elif isinstance(v, (list, tuple)):
            self.out += b"[" + _long(len(v))
            for item in v:
                self.value(item)
        elif isinstance(v, dict):
            self.out += b"{" + _long(len(v))
            for key, item in v.items():
                self.value(key)
                self.value(item)
        elif isinstance(v, RVersion):
            self._user_marshal("Gem::Version", [v.text])
        elif isinstance(v, RRequirement):
            self._user_marshal(
                "Gem::Requirement", [[[op, RVersion(version)] for op, version in v.parts]]
            )
        elif isinstance(v, RDependency):
            self.out += b"o"
            self.symbol("Gem::Dependency")
            self.out += _long(5)
            self.symbol("@name")
            self.string(v.name)
            self.symbol("@requirement")
            self.value(v.requirement)
            self.symbol("@type")
            self.symbol(v.kind)
            self.symbol("@prerelease")
            self.out += b"F"
            self.symbol("@version_requirements")
            self.value(v.requirement)
        elif isinstance(v, RTime):
            self._time(v.value)
        else:
            raise TypeError(f"cannot marshal {type(v).__name__}")

    def _user_marshal(self, class_name: str, payload: Any) -> None:
        self.out += b"U"
        self.symbol(class_name)
        self.value(payload)

    def _time(self, moment: datetime) -> None:
        moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment
        high = (
            (1 << 31)
            | (1 << 30)  # UTC flag
            | ((moment.year - 1900) << 14)
            | ((moment.month - 1) << 10)
            | (moment.day << 5)
            | moment.hour
        )
        low = (moment.minute << 26) | (moment.second << 20) | moment.microsecond
        raw = high.to_bytes(4, "little") + low.to_bytes(4, "little")
        self.out += b"Iu"
        self.symbol("Time")
        self.out += _long(len(raw)) + raw + _long(1)
        self.symbol("zone")
        self.out += b'I"' + _long(3) + b"UTC" + _long(1)
        self.symbol("E")
        self.out += b"F"


def dump(value: Any) -> bytes:
    writer = _Writer()
    writer.value(value)
    return bytes(writer.out)


def specification_dump(spec: SpecData) -> bytes:
    """``Marshal.dump`` of a ``Gem::Specification`` (platform ``ruby`` only)."""
    moment = spec.date or datetime.now(timezone.utc)
    midnight = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    fields = [
        spec.rubygems_version,
        SPECIFICATION_VERSION,
        spec.name,
        RVersion(spec.version),
        RTime(midnight),
        spec.summary,
        spec.required_ruby_version,
        spec.required_rubygems_version,
        None,  # original_platform is nil for the default platform
        list(spec.dependencies),
        "",  # rubyforge_project
        spec.email,
        list(spec.authors),
        spec.description,
        spec.homepage,
        True,  # has_rdoc
        "ruby",
        list(spec.licenses),
        dict(spec.metadata),
    ]
    inner = dump(fields)
    outer = _Writer()
    outer.out += b"u"
    outer.symbol("Gem::Specification")
    outer.out += _long(len(inner)) + inner
    return bytes(outer.out)


def quick_spec(spec: SpecData) -> bytes:
    """The body of ``/quick/Marshal.4.8/<name>-<version>.gemspec.rz``."""
    return zlib.compress(specification_dump(spec))
