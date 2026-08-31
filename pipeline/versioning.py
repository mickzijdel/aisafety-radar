"""Pack and plugin version strings: `YYYY.MM.DDx` packs, `YYYY.MDD.N` plugin manifests."""

from __future__ import annotations

import re
import string
from collections.abc import Iterable
from datetime import date

_PACK_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})([a-z])$")


def pack_version(day: date, letter: str = "a") -> str:
    if letter not in string.ascii_lowercase:
        raise ValueError(f"letter must be a-z, got {letter!r}")
    return f"{day:%Y.%m.%d}{letter}"


def parse_pack_version(pack: str) -> tuple[date, str]:
    m = _PACK_RE.match(pack)
    if not m:
        raise ValueError(f"not a pack version: {pack!r}")
    y, mo, d, letter = m.groups()
    return date(int(y), int(mo), int(d)), letter


def next_letter(existing_versions: Iterable[str], day: date) -> str:
    used = set()
    for v in existing_versions:
        try:
            d, letter = parse_pack_version(v)
        except ValueError:
            continue
        if d == day:
            used.add(letter)
    for letter in string.ascii_lowercase:
        if letter not in used:
            return letter
    raise RuntimeError(f"more than 26 versions on {day}")


def plugin_version(pack: str) -> str:
    day, letter = parse_pack_version(pack)
    return f"{day.year}.{day.month}{day.day:02d}.{string.ascii_lowercase.index(letter) + 1}"
