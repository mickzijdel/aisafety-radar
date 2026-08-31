"""The `radar` command line: a fixture dry run, the probe table, and the exit codes.

Every test here stays off the network: `--fixtures` swaps the forum clients for `FixtureClient`,
`--dry-run` swaps the model for `CannedLLM`, and the probe test replaces the client factory.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.cli import main
from pipeline.fetchers.fixtures import FixtureClient
from pipeline.models import GateResult, Usage
from pipeline.run import RunReport

FIX = "tests/fixtures/forummagnum"


def _run(tmp_path, *extra):
    return main(
        ["run", "--dry-run", "--fixtures", FIX, "--data", str(tmp_path / "data"), "--out"]
        + [str(tmp_path / "out"), *extra]
    )


def test_cli_dry_run_with_fixtures(tmp_path):
    code = _run(tmp_path)
    assert code == 0
    assert (tmp_path / "out" / "00-index.md").exists()
    assert (tmp_path / "out" / "31-today-forums.md").exists()


def test_cli_dry_run_prints_the_run_report_and_dates_the_pack_from_the_fixtures(tmp_path, capsys):
    _run(tmp_path)
    report = json.loads(capsys.readouterr().out)
    day = FixtureClient.day_of(Path(FIX))
    # No changelog yet, so this is the day's first pack.
    assert report["version"] == f"{day:%Y.%m.%d}a"
    assert report["published"] is True
    assert report["items_selected"] > 0
    assert (tmp_path / "data" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "run.json").exists()


def test_cli_takes_an_explicit_day_and_version(tmp_path, capsys):
    code = _run(tmp_path, "--day", "2026-08-31", "--version", "2026.08.31c")
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["version"] == "2026.08.31c"
    assert (tmp_path / "data" / "2026" / "08" / "31" / "run.json").exists()


def test_cli_picks_the_next_letter_from_the_changelog(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    day = FixtureClient.day_of(Path(FIX))
    changelog.write_text(f"# Changelog\n\n## {day:%Y.%m.%d}a ({day})\n\n- first pack\n")
    _run(tmp_path, "--changelog", str(changelog))
    assert json.loads(capsys.readouterr().out)["version"] == f"{day:%Y.%m.%d}b"


def test_cli_ignore_seen_regenerates_a_day_that_already_published(tmp_path, capsys):
    # A published run marks its items seen, so an ordinary rerun of the same day finds nothing
    # left to render and produces an empty pack. That is the whole recovery problem: a day whose
    # generation succeeded but whose promote failed has no way back without this flag.
    assert _run(tmp_path) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["items_selected"] > 0

    assert _run(tmp_path) == 0
    stale = json.loads(capsys.readouterr().out)
    assert stale["items_selected"] == 0

    assert _run(tmp_path, "--ignore-seen") == 0
    again = json.loads(capsys.readouterr().out)
    assert again["items_selected"] == first["items_selected"]
    assert (tmp_path / "out" / "31-today-forums.md").read_text().count("\n- **") > 0


def test_cli_exits_3_when_a_blocking_gate_failed(tmp_path, monkeypatch):
    report = RunReport(
        version="2026.08.31a",
        statuses=[],
        items_fetched=0,
        items_after_floor=0,
        items_selected=0,
        rendered_files=[],
        gates=[GateResult(name="provenance", passed=False, blocking=True)],
        usage=Usage(),
        movers=0,
        published=False,
    )
    monkeypatch.setattr("pipeline.cli.run_daily", lambda inputs: report)
    assert _run(tmp_path) == 3


def test_cli_probe_prints_a_status_table_and_always_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(
        "pipeline.cli._clients",
        lambda fixtures, ua: {s: FixtureClient(s, Path(FIX)) for s in ("lw", "af", "eaf")},
    )
    assert main(["probe", "--hours", "24"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("| source | ok | items | error |")
    assert "| lesswrong | yes |" in out
