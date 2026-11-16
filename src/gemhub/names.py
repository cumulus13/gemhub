"""Validation of names taken from URLs before they reach the filesystem or upstream."""

from __future__ import annotations

import re
from typing import List, Tuple

GEM_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")
GEM_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,290}\.gem$")
QUICK_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,290}\.gemspec\.rz$")
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._+-]{0,127}$")


def valid_gem_name(name: str) -> bool:
    return bool(GEM_NAME_RE.match(name)) and ".." not in name


def valid_gem_filename(filename: str) -> bool:
    return bool(GEM_FILE_RE.match(filename)) and ".." not in filename


def valid_quick_filename(filename: str) -> bool:
    return bool(QUICK_FILE_RE.match(filename)) and ".." not in filename


def split_candidates(stem: str) -> List[Tuple[str, str]]:
    """Possible ``(gem_name, version_label)`` splits of ``name-version[-platform]``.

    Gem versions start with a digit, but names may contain ``-<digit>`` too, so every
    ``-`` followed by a digit is a candidate. Longest name first, since a version label
    rarely contains ``-<digit>`` while names sometimes do.
    """
    out: List[Tuple[str, str]] = []
    for index, char in enumerate(stem):
        if char == "-" and index > 0 and index + 1 < len(stem) and stem[index + 1].isdigit():
            out.append((stem[:index], stem[index + 1 :]))
    out.reverse()
    return out
