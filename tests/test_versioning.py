import string
from datetime import date

import pytest

from pipeline.versioning import next_letter, pack_version, parse_pack_version, plugin_version


def test_pack_version_format():
    assert pack_version(date(2026, 8, 30)) == "2026.08.30a"
    assert pack_version(date(2026, 1, 5), "c") == "2026.01.05c"


def test_pack_version_rejects_non_single_letters():
    with pytest.raises(ValueError):
        pack_version(date(2026, 8, 30), "")
    with pytest.raises(ValueError):
        pack_version(date(2026, 8, 30), "ab")


def test_next_letter_skips_existing_same_day():
    existing = ["2026.08.29a", "2026.08.30a", "2026.08.30b"]
    assert next_letter(existing, date(2026, 8, 30)) == "c"
    assert next_letter(existing, date(2026, 8, 31)) == "a"


def test_next_letter_never_reuses_a_lower_letter_after_a_gap():
    # A gap must not send the manifest version backwards: after "b" the next pack is "c",
    # never "a", even though "a" is unused.
    assert next_letter(["2026.08.30b"], date(2026, 8, 30)) == "c"


def test_next_letter_raises_when_the_day_is_exhausted():
    day = date(2026, 8, 30)
    existing = [pack_version(day, letter) for letter in string.ascii_lowercase]
    with pytest.raises(RuntimeError):
        next_letter(existing, day)


def test_plugin_version_is_semver_shaped():
    assert plugin_version("2026.08.30a") == "2026.830.1"
    assert plugin_version("2026.01.05c") == "2026.105.3"
    assert plugin_version("2026.12.01a") == "2026.1201.1"


def test_parse_roundtrip():
    assert parse_pack_version("2026.08.30b") == (date(2026, 8, 30), "b")
    with pytest.raises(ValueError):
        parse_pack_version("2026-08-30")


def test_parse_rejects_a_trailing_newline():
    with pytest.raises(ValueError):
        parse_pack_version("2026.08.30a\n")
