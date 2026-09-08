"""The publishing helpers: changelog, manifests, health and the published site.

Four contracts here are load-bearing outside this file and are pinned exactly rather than
loosely:

* `checksums.txt` is `sha256sum` text format -- 64 hex, *two* spaces, the bare file name --
  because `skills/aisafety-radar/scripts/fetch-latest.sh` reads it with `awk '$2 == n'`. A path
  in column 2, or one space, silently breaks every freshness fetch.
* A changelog heading has to round-trip through `pipeline.cli._existing_versions` and
  `pipeline.versioning.next_letter`, which is how the next day's pack gets its letter. A heading
  shape those two cannot parse would restart every day at `a` and make the plugin version go
  backwards.
* `site.BUNDLES` has to agree with `pipeline.render.RECIPES`: the index advertises a recipe's
  file list and the bundle is meant to be exactly that list in one fetch.
* `health.refusal_reason` is what the daily workflow's guard step calls, so it is tested here
  rather than left as untested YAML.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml

from pipeline.cli import _existing_versions
from pipeline.publish import health
from pipeline.publish.changelog import prepend, versions
from pipeline.publish.health import ping, refusal_reason, render_issue_body, update_pinned_issue
from pipeline.publish.manifests import bump
from pipeline.publish.site import BUNDLES, KEEP_VERSIONS, write_latest
from pipeline.render import APPENDIX_PURPOSE, RECIPES
from pipeline.versioning import next_letter

CHECKSUM_LINE = re.compile(r"[0-9a-f]{64} {2}[^ /\\]+")


def _report(**overrides) -> dict:
    report = {
        "version": "2026.08.30a",
        "statuses": [
            {"source": "lesswrong", "category": "forums", "ok": True, "items": 12, "error": None},
            {"source": "eaforum", "category": "forums", "ok": False, "items": 0, "error": "403"},
        ],
        "gates": [{"name": "provenance", "passed": True, "blocking": True, "details": []}],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cost_usd": 0.42,
        },
        "items_selected": 3,
        "dropped": [],
        "scan_flagged": [],
        "failed": [],
        "published": True,
    }
    return report | overrides


# --- changelog ----------------------------------------------------------------------------


def test_changelog_prepend_and_versions(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    prepend(path, "2026.08.30a", date(2026, 8, 30), ["31-today-forums.md: 12 posts, 2 movers"])
    prepend(path, "2026.08.31a", date(2026, 8, 31), ["31-today-forums.md: 9 posts"], keep=30)
    assert versions(path) == ["2026.08.31a", "2026.08.30a"]
    for n in range(40):
        prepend(
            path, f"2026.09.{n % 28 + 1:02d}{'abcdef'[n % 6]}", date(2026, 9, 1), ["x"], keep=30
        )
    assert len(versions(path)) == 30


def test_changelog_keeps_the_bullets_of_the_entries_it_retains(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    prepend(path, "2026.08.30a", date(2026, 8, 30), ["first line", "second line"])
    prepend(path, "2026.08.31a", date(2026, 8, 31), ["newest"])
    text = path.read_text()
    assert text.startswith("# Changelog\n")
    assert "## 2026.08.31a (2026-08-31)\n- newest\n" in text
    assert "## 2026.08.30a (2026-08-30)\n- first line\n- second line\n" in text
    # Newest first, and the header is not mistaken for an entry.
    assert text.index("2026.08.31a") < text.index("2026.08.30a")


def test_changelog_round_trips_through_the_version_parser(tmp_path):
    """prepend -> `_existing_versions` -> `next_letter` is how tomorrow's pack is named."""
    path = tmp_path / "CHANGELOG.md"
    day = date(2026, 8, 30)
    assert next_letter(_existing_versions(path), day) == "a"
    prepend(path, "2026.08.30a", day, ["first pack"])
    assert _existing_versions(path) == ["2026.08.30a"]
    assert next_letter(_existing_versions(path), day) == "b"
    prepend(path, "2026.08.30b", day, ["second pack"])
    assert _existing_versions(path) == ["2026.08.30b", "2026.08.30a"]
    assert next_letter(_existing_versions(path), day) == "c"
    # A new day starts at `a` again, even with yesterday's entries still in the file.
    assert next_letter(_existing_versions(path), date(2026, 8, 31)) == "a"


def test_prepending_the_same_version_twice_rewrites_one_entry(tmp_path):
    """The promote job is re-runnable, so a second pass must not double the heading."""
    path = tmp_path / "CHANGELOG.md"
    day = date(2026, 8, 30)
    prepend(path, "2026.08.30a", day, ["first attempt"])
    prepend(path, "2026.08.30a", day, ["second attempt"])
    assert versions(path) == ["2026.08.30a"]
    text = path.read_text()
    assert text.count("## 2026.08.30a") == 1
    assert "- second attempt\n" in text and "first attempt" not in text
    # And the letter for the next pack is still `b`, not `c`.
    assert next_letter(_existing_versions(path), day) == "b"


def test_prepending_a_repeat_version_keeps_the_other_entries(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    prepend(path, "2026.08.30a", date(2026, 8, 30), ["older"])
    prepend(path, "2026.08.31a", date(2026, 8, 31), ["newer"])
    prepend(path, "2026.08.31a", date(2026, 8, 31), ["newer, retried"])
    assert versions(path) == ["2026.08.31a", "2026.08.30a"]
    assert "- older\n" in path.read_text()


def test_versions_of_a_missing_changelog_is_empty(tmp_path):
    assert versions(tmp_path / "nope.md") == []


# --- manifests ----------------------------------------------------------------------------


def test_bump_manifests(tmp_path):
    plugin = tmp_path / "plugin.json"
    market = tmp_path / "marketplace.json"
    plugin.write_text(json.dumps({"name": "aisafety-radar", "version": "0.0.1"}))
    market.write_text(
        json.dumps(
            {
                "name": "m",
                "version": "0.0.1",
                "plugins": [{"name": "aisafety-radar", "version": "0.0.1"}],
            }
        )
    )
    assert bump("2026.08.30b", [plugin, market]) == "2026.830.2"
    assert json.loads(plugin.read_text())["version"] == "2026.830.2"
    assert json.loads(market.read_text())["plugins"][0]["version"] == "2026.830.2"


def test_bump_keeps_every_other_key_and_ends_with_a_newline(tmp_path):
    path = tmp_path / "plugin.json"
    path.write_text(
        json.dumps({"name": "aisafety-radar", "version": "0.0.1", "skills": "./skills"})
    )
    bump("2026.01.05a", [path])
    assert json.loads(path.read_text()) == {
        "name": "aisafety-radar",
        "version": "2026.105.1",
        "skills": "./skills",
    }
    assert path.read_text().endswith("}\n")


def test_bump_rejects_a_version_that_is_not_a_pack_version(tmp_path):
    path = tmp_path / "plugin.json"
    path.write_text(json.dumps({"version": "0.0.1"}))
    with pytest.raises(ValueError, match="not a pack version"):
        bump("2026.08.30", [path])
    # Nothing was written before the failure.
    assert json.loads(path.read_text())["version"] == "0.0.1"


# --- health -------------------------------------------------------------------------------


def test_issue_body_lists_sources_and_gates():
    body = render_issue_body(_report())
    assert "| eaforum | NO |" in body and "$0.42" in body and "provenance" in body


def test_issue_body_reports_dropped_failed_and_scan_flagged():
    body = render_issue_body(
        _report(items_selected=5, dropped=["a", "b", "c"], scan_flagged=["a"], failed=["b", "c"])
    )
    assert "dropped | 3 | a, b, c" in body
    assert "scan-flagged | 1 | a" in body
    assert "failed | 2 | b, c" in body


def test_issue_body_names_the_outcome_and_links_the_run():
    body = render_issue_body(
        _report(published=False), outcome="gates blocked", run_url="http://x/1"
    )
    assert "gates blocked" in body
    assert "http://x/1" in body
    assert "Published: NO" in body


def test_issue_body_survives_a_report_from_a_crashed_run():
    body = render_issue_body({}, outcome="run crashed")
    assert "run crashed" in body
    assert "$0.00" in body


def _minimums(passed, details):
    return {"name": "source-minimums", "passed": passed, "blocking": False, "details": details}


def test_issue_body_gives_the_7_day_minimums_a_row_per_shortfall():
    body = render_issue_body(
        _report(
            gates=[
                _minimums(
                    False,
                    [
                        "lesswrong: 7 items in 7 days, below the minimum 15",
                        "category forums: 7 items in 7 days, below the minimum 23",
                    ],
                )
            ]
        )
    )
    assert "## 7-day minimums" in body
    section = body.split("## 7-day minimums")[1]
    assert "| lesswrong | 7 items in 7 days, below the minimum 15 |" in section
    assert "| category forums | 7 items in 7 days, below the minimum 23 |" in section


def test_issue_body_says_so_when_every_source_cleared_its_7_day_minimum():
    body = render_issue_body(_report(gates=[_minimums(True, [])]))
    assert "## 7-day minimums" in body
    assert "| every source and category | above its 7-day minimum |" in body


def test_issue_body_passes_on_the_gates_own_words_when_there_is_no_week_yet():
    """A passing gate must not render a row that reads like a shortfall.

    The insufficient-history line carries a colon of its own, so the `"<name>: <what went
    wrong>"` split turns it into `| 3 of the last 7 runs recorded | too little history ... |`,
    which is a source name and a count to anyone skimming the section. Anchored inside the
    section rather than the whole body: the generic Gates table prints the same detail verbatim,
    so a whole-body assertion passes even with `_minimum_rows` deleted.
    """
    detail = "3 of the last 7 runs recorded: too little history to judge a week"
    body = render_issue_body(_report(gates=[_minimums(True, [detail])]))
    section = body.split("## 7-day minimums")[1]
    assert f"| {detail} |" in section
    assert "| 3 of the last 7 runs recorded |" not in section


def test_issue_body_never_splits_a_passing_minimums_detail_into_two_columns():
    """Any passing detail is prose, not a shortfall, however many colons it happens to carry."""
    detail = "note: everything above minimum: nothing to do"
    body = render_issue_body(_report(gates=[_minimums(True, [detail])]))
    section = body.split("## 7-day minimums")[1]
    assert f"| {detail} |" in section


def test_issue_body_says_the_minimums_were_not_checked_when_the_gate_is_absent():
    # A report from before this gate existed, or from a run that crashed before the gates ran.
    body = render_issue_body(_report(gates=[]))
    assert "## 7-day minimums" in body
    assert "not checked" in body


def test_refusal_reason_accepts_a_run_that_rendered_items():
    assert refusal_reason(_report(items_selected=3, dropped=["a"], failed=["a"])) is None


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ({}, "no report"),
        (_report(published=False), "did not publish"),
        # A same-day rerun: everything is already seen, so nothing was even selected.
        (_report(items_selected=0, dropped=[]), "0 rendered items"),
        # Every selected item raised during extract/scan/judge.
        (
            _report(items_selected=2, dropped=["a", "b"], failed=["a", "b"]),
            "0 rendered items",
        ),
    ],
)
def test_refusal_reason_refuses_an_empty_or_blocked_run(report, expected):
    reason = refusal_reason(report)
    assert reason is not None and expected in reason


def test_refusal_reason_counts_dropped_not_failed_so_a_scan_wave_cannot_publish_empty():
    """An injection-scan wave empties the render set without putting one id in `failed`.

    `RunReport.dropped` is the union of every selected item that never reached the render set;
    `failed` is only its exception branch. Subtracting `failed` would read this run as five
    healthy items and push an empty pack to the `generated` branch.
    """
    ids = ["a", "b", "c", "d", "e"]
    reason = refusal_reason(
        _report(published=True, items_selected=5, failed=[], scan_flagged=ids, dropped=ids)
    )
    assert reason is not None
    assert "0 rendered items" in reason
    assert "5 dropped" in reason and "5 scan-flagged" in reason


def test_refusal_reason_refuses_a_report_with_no_dropped_list():
    """`dropped` is the subtrahend, so an absent key would read as zero drops.

    A report shaped differently -- an older run, a truncated write, a schema change -- is a
    report this guard cannot verify, and "cannot verify" has to mean refuse.
    """
    report = _report()
    del report["dropped"]
    reason = refusal_reason(report)
    assert reason is not None and "dropped" in reason


def test_update_pinned_issue_edits_the_existing_issue(monkeypatch):
    calls = []

    class _Done:
        stdout = "17\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Done()

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    update_pinned_issue("o/r", "body")
    assert calls[0][:3] == ["gh", "issue", "list"]
    assert calls[1][:3] == ["gh", "issue", "edit"]
    assert "17" in calls[1] and "body" in calls[1]


def test_update_pinned_issue_creates_one_when_none_is_open(monkeypatch):
    calls = []

    class _Empty:
        stdout = "\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Empty()

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    update_pinned_issue("o/r", "body", title="Pipeline health")
    assert calls[1][:3] == ["gh", "issue", "create"]
    assert health.LABEL in calls[1]


def test_ping_does_nothing_without_a_url(monkeypatch):
    monkeypatch.setattr(health.httpx, "get", lambda *a, **k: pytest.fail("ping called with no url"))
    ping(None)
    ping("")


def test_ping_swallows_a_failed_request(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(health.httpx, "get", boom)
    ping("https://hc.example/uuid")  # must not raise: a dead-man's switch is not a gate
    assert "healthchecks ping failed" in caplog.text


def test_ping_gets_the_url(monkeypatch):
    seen = []
    monkeypatch.setattr(health.httpx, "get", lambda url, **k: seen.append(url))
    ping("https://hc.example/uuid")
    assert seen == ["https://hc.example/uuid"]


# --- the daily workflow's own shell -------------------------------------------------------


DAILY_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "daily.yml"
# `-e` on its own or bundled into a short-option cluster (`-er`, `-re`, ...).
_JQ_EXIT_STATUS = re.compile(r"\bjq\s+(?:--exit-status\b|-[a-zA-Z]*e[a-zA-Z]*\b)")


def _run_bodies(workflow: Path) -> list[tuple[str, str]]:
    """Every step's `run:` script in the workflow, as (step name, script).

    Parsed rather than grepped, so the header comment that *explains* the rule below is not
    mistaken for a violation of it.
    """
    doc = yaml.safe_load(workflow.read_text())
    return [
        (step.get("name") or "(unnamed)", step["run"])
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and "run" in step
    ]


def test_no_step_in_the_daily_workflow_uses_jq_exit_status():
    """`jq -e` exits 1 when the value it printed was `false` or `null`.

    So `jq -e ... || echo false` on a *valid* blocked report prints "false" twice, and the stray
    second line makes GitHub reject the whole $GITHUB_OUTPUT file -- failing the step on exactly
    the path it exists to handle. The workflow's header comment says so; nothing enforced it.
    """
    bodies = _run_bodies(DAILY_WORKFLOW)
    assert any("jq " in body for _, body in bodies), "no jq call left to guard"
    for name, body in bodies:
        found = _JQ_EXIT_STATUS.search(body)
        assert not found, f"{name}: jq exit-status flag in a run body: {found.group(0)!r}"


# --- site ---------------------------------------------------------------------------------


def _pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "31-today-forums.md").write_text("forums\n")
    (pack / "00-index.md").write_text("index\n")
    return pack


def test_write_latest_builds_bundle_and_checksums(tmp_path):
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    write_latest(pack, site, "2026.08.30a")
    latest = site / "latest"
    assert (latest / "today.md").read_text() == "forums\n"
    sums = (latest / "checksums.txt").read_text()
    assert "31-today-forums.md" in sums and "today.md" in sums
    assert (site / "versions" / "2026.08.30a" / "00-index.md").exists()
    assert "31-today-forums.md" in (site / "llms.txt").read_text()


def test_checksums_are_sha256sum_text_format(tmp_path):
    """`fetch-latest.sh` reads column 2 with awk, so it must be the bare name after two spaces."""
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    write_latest(pack, site, "2026.08.30a")
    latest = site / "latest"
    text = (latest / "checksums.txt").read_text()
    assert text.endswith("\n")
    names = []
    for line in text.splitlines():
        assert CHECKSUM_LINE.fullmatch(line), f"not sha256sum text format: {line!r}"
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((latest / name).read_bytes()).hexdigest()
        names.append(name)
    assert names == sorted(names)
    assert {"00-index.md", "31-today-forums.md", "today.md"} <= set(names)


def test_only_bundles_whose_parts_exist_are_written(tmp_path):
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    write_latest(pack, site, "2026.08.30a")
    latest = site / "latest"
    assert (latest / "today.md").exists()
    # Nothing in the pack feeds week/careers/map yet, so no empty bundle is published.
    assert not (latest / "week.md").exists()
    assert not (latest / "map.md").exists()


def test_write_latest_replaces_a_stale_latest(tmp_path):
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    write_latest(pack, site, "2026.08.30a")
    (site / "latest" / "99-gone.md").write_text("stale\n")
    write_latest(pack, site, "2026.08.31a")
    assert not (site / "latest" / "99-gone.md").exists()
    # Both versions stay archived.
    assert (site / "versions" / "2026.08.30a" / "00-index.md").exists()
    assert (site / "versions" / "2026.08.31a" / "00-index.md").exists()


def test_the_version_archive_keeps_the_newest_30_the_way_the_changelog_does(tmp_path):
    """`versions/` and CHANGELOG.md describe the same list of packs, so they keep the same number
    of them. Unbounded, `versions/` grows by a full copy of the pack every day forever in a repo
    that is also a published site."""
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    for n in range(31):
        write_latest(pack, site, f"2026.{n // 28 + 8:02d}.{n % 28 + 1:02d}a")
    kept = sorted(p.name for p in (site / "versions").iterdir())
    assert len(kept) == KEEP_VERSIONS == 30
    # A pack version sorts chronologically as a string, so the oldest is the one that went.
    assert "2026.08.01a" not in kept
    assert kept[0] == "2026.08.02a" and kept[-1] == "2026.09.03a"
    # The survivors are whole packs, not emptied directories.
    assert (site / "versions" / kept[-1] / "00-index.md").read_text() == "index\n"


def test_the_version_archive_prunes_nothing_below_the_limit(tmp_path):
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    for day in range(1, 4):
        write_latest(pack, site, f"2026.08.{day:02d}a")
    assert len(list((site / "versions").iterdir())) == 3


def test_llms_txt_gives_every_file_a_purpose_and_a_working_relative_link(tmp_path):
    pack = _pack(tmp_path)
    site = tmp_path / "site"
    write_latest(pack, site, "2026.08.30a")
    text = (site / "llms.txt").read_text()
    assert "2026.08.30a" in text
    forums = "31-today-forums.md"
    assert f"- [{forums}](latest/{forums}): {APPENDIX_PURPOSE[forums]}" in text
    assert "- [today.md](latest/today.md): bundle" in text
    assert "checksums.txt" in text
    for line in text.splitlines():
        m = re.fullmatch(r"- \[[^\]]+\]\((latest/[^)]+)\): .+", line)
        if m:
            assert (site / m.group(1)).exists(), m.group(1)


def test_bundles_match_the_render_recipes():
    """A bundle is a recipe in one fetch, so the two file lists have one definition."""
    recipes = dict(RECIPES)
    assert BUNDLES["today.md"] == recipes["what happened today"]
    assert BUNDLES["week.md"] == recipes["this week"]
    assert BUNDLES["careers.md"] == recipes["career change"]
    # The four names `fetch-latest.sh` accepts as arguments. SKILL.md advertises only
    # the ones a phase actually publishes; see tests/test_skill.py.
    assert set(BUNDLES) == {"today.md", "week.md", "careers.md", "map.md"}
    for parts in BUNDLES.values():
        for name in parts:
            assert name in APPENDIX_PURPOSE, name
