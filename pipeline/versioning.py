"""Pack and plugin version strings: `YYYY.MM.DDx` packs, `YYYY.MDD.N` plugin manifests."""

from __future__ import annotations

import re
import string
from collections.abc import Iterable
from datetime import date

_PACK_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})([a-z])")


def pack_version(day: date, letter: str = "a") -> str:
    # `in` on a str is a substring test, so the length check is what rejects "" and "ab".
    if len(letter) != 1 or letter not in string.ascii_lowercase:
        raise ValueError(f"letter must be a-z, got {letter!r}")
    return f"{day:%Y.%m.%d}{letter}"


def parse_pack_version(pack: str) -> tuple[date, str]:
    # fullmatch, not match with `$`: `$` also matches before a trailing newline.
    m = _PACK_RE.fullmatch(pack)
    if not m:
        raise ValueError(f"not a pack version: {pack!r}")
    y, mo, d, letter = m.groups()
    return date(int(y), int(mo), int(d)), letter


def next_letter(existing_versions: Iterable[str], day: date) -> str:
    """Return the letter for the next pack published on `day`.

    Entries that are not pack versions are silently skipped, so the caller can pass a raw
    directory listing or tag list. The result always advances past the highest letter already
    used that day rather than filling a gap, because the plugin version derived from it must
    never go backwards.
    """
    used = set()
    for v in existing_versions:
        try:
            d, letter = parse_pack_version(v)
        except ValueError:
            continue
        if d == day:
            used.add(letter)
    if not used:
        return "a"
    highest = max(used)
    if highest == "z":
        raise RuntimeError(f"more than 26 versions on {day}")
    return string.ascii_lowercase[string.ascii_lowercase.index(highest) + 1]


def plugin_version(pack: str) -> str:
    # Compare these numerically per component, never as strings: month-day is unpadded, so
    # "2026.105.1" (Jan 5) sorts after "2026.1201.1" (Dec 1) lexically but before it numerically.
    day, letter = parse_pack_version(pack)
    return f"{day.year}.{day.month}{day.day:02d}.{string.ascii_lowercase.index(letter) + 1}"
