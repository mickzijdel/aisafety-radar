"""The `radar` command line: a fixture dry run, the probe table, and the exit codes.

Every test here stays off the network: `--fixtures` swaps the forum clients for `FixtureClient`,
`--dry-run` swaps the model for `CannedLLM`, and the probe test replaces the client factory.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pipeline.run
from pipeline.cli import main, probe_call_shapes
from pipeline.fetchers.fixtures import FixtureClient
from pipeline.fetchers.forummagnum import POST_FIELDS_LEAN
from pipeline.models import GateResult, Usage
from pipeline.run import RunReport, _window_posts

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


class _BrokenWindow(FixtureClient):
    """Serves the daily fetch and refuses the lean window call, the way a WAF would."""

    def posts_since(self, after, view="top", limit=200, require_ai_tag=True, **kw):
        if view == "new":
            raise RuntimeError("403 Forbidden")
        return super().posts_since(
            after, view=view, limit=limit, require_ai_tag=require_ai_tag, **kw
        )


def _probe_clients(monkeypatch, overrides=None):
    clients = {s: FixtureClient(s, Path(FIX)) for s in ("lw", "af", "eaf")}
    clients.update(overrides or {})
    monkeypatch.setattr("pipeline.cli._clients", lambda fixtures, ua: clients)


def test_cli_probe_also_exercises_the_window_and_top_comment_call_shapes(monkeypatch, capsys):
    """The daily fetch is `posts_since(view="top")` with the full field set. Production makes two
    other shapes every morning - the lean `view="new"` window fetch and `postCommentsTop` - and
    neither was ever sent from a runner before go-live."""
    _probe_clients(monkeypatch)
    assert main(["probe", "--hours", "24"]) == 0
    out = capsys.readouterr().out
    for site in ("lw", "af", "eaf"):
        assert f"| {site}-window | yes |" in out
        assert f"| {site}-comments |" in out


def test_cli_probe_reports_a_failing_window_call_as_a_row_and_still_exits_0(monkeypatch, capsys):
    _probe_clients(monkeypatch, {"lw": _BrokenWindow("lw", Path(FIX))})
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "| lw-window | NO | 0 | 403 Forbidden |" in out
    # The other sites are unaffected, and the daily fetch table is still there.
    assert "| af-window | yes |" in out
    assert "| lesswrong | yes |" in out


class _RecordsItsCalls:
    """A client that answers nothing and remembers exactly how it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []

    def posts_since(self, after, **kwargs):
        self.calls.append((after, kwargs))
        return []

    def top_comments(self, post_id):  # pragma: no cover - no post to ask about
        return []


def _window_calls(fn) -> dict[str, tuple[object, dict]]:
    """The window `posts_since` call `fn` makes per site, as (after, kwargs)."""
    clients = {site: _RecordsItsCalls() for site in ("lw", "af", "eaf")}
    fn(clients, date(2026, 8, 30))
    return {site: client.calls[0] for site, client in clients.items()}


def test_the_probe_sends_the_window_call_production_sends(monkeypatch):
    """The probe exists to prove the two shapes the daily fetch never sends still work. It proves
    nothing if it sends a shape production does not: the kwargs were hand-copied from `run.py`,
    and nothing kept the copies in step. Both call sites now go through one helper, and this
    fails if either grows its own copy again."""
    assert _window_calls(probe_call_shapes) == _window_calls(_window_posts)

    # And they agree because there is one definition, not two that happen to match today: a
    # change to it has to reach both call sites.
    monkeypatch.setattr(
        pipeline.run, "WINDOW_FETCH_KWARGS", {"view": "changed", "limit": 7, "fields": ["_id"]}
    )
    probed, produced = _window_calls(probe_call_shapes), _window_calls(_window_posts)
    assert probed == produced
    assert produced["lw"][1] == {
        "view": "changed",
        "limit": 7,
        "fields": ["_id"],
        "require_ai_tag": True,
    }


def test_the_window_call_asks_the_af_without_an_ai_tag_filter():
    """The AF is an AI-safety forum end to end and carries no AI tag to filter on, so the one
    kwarg that is not shared between sites is the one that must stay per site."""
    calls = _window_calls(_window_posts)
    assert calls["af"][1]["require_ai_tag"] is False
    assert calls["lw"][1]["require_ai_tag"] is True and calls["eaf"][1]["require_ai_tag"] is True
    # Every site is asked for the same 14 days back, in the lean field set.
    assert {after for after, _ in calls.values()} == {date(2026, 8, 30) - timedelta(days=14)}
    assert calls["lw"][1]["view"] == "new" and calls["lw"][1]["fields"] == POST_FIELDS_LEAN
