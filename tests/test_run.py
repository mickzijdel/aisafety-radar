"""The daily orchestration: `run_daily` wiring every stage together against recorded fixtures.

The two end-to-end tests are the point of this file. Everything else pins one wiring decision
that only shows up when the modules are joined: which item list each gate sees, what happens
when the comment refresh or a window fetch fails, and that a rerun of the same day does not
double the archive.

`CannedLLM` is exercised here rather than in `tests/test_llm.py` because what has to hold about
it is that the *stages* accept its answers -- the schemas it dispatches on are theirs, not this
module's.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.archive import Archive
from pipeline.config import load_settings
from pipeline.fetchers.base import Window
from pipeline.fetchers.fixtures import FixtureClient
from pipeline.fetchers.forummagnum import RawComment, RawPost
from pipeline.fetchers.forums import ForumFetcher, raw_post_to_item
from pipeline.llm import CannedLLM
from pipeline.models import Extraction, Item
from pipeline.run import RunInputs, _source_medians, _trailing_cost_median, run_daily
from pipeline.stages.comments import Mover
from pipeline.stages.extract import extract_verified
from pipeline.stages.scan import judge_record, scan_record
from pipeline.stages.triage import triage_item
from pipeline.stages.verify import ExtractionRecord
from pipeline.textnorm import contains

FIX = Path("tests/fixtures/forummagnum")
SITES = ("lw", "af", "eaf")


def _fetchers(clients):
    return [
        ForumFetcher("lw", clients["lw"], "lesswrong", 15),
        ForumFetcher("af", clients["af"], "alignmentforum", 3),
        ForumFetcher("eaf", clients["eaf"], "eaforum", 5),
    ]


def _inputs(tmp_path, clients, llm=None, fetchers=None, **kw):
    return RunInputs(
        settings=load_settings(),
        llm=llm or CannedLLM(),
        archive=Archive(tmp_path / "data"),
        clients=clients,
        fetchers=fetchers if fetchers is not None else _fetchers(clients),
        day=FixtureClient.day_of(FIX),
        version="2026.08.30a",
        out_dir=tmp_path / "out",
        **kw,
    )


def _clients():
    return {s: FixtureClient(s, FIX) for s in SITES}


def _item(
    text: str = "The team ran 3 trials on 2026-08-30. The second point adds detail. A third.",
) -> Item:
    return Item(
        id="lw:abc123",
        source="lesswrong",
        site="lw",
        category="forums",
        url="https://www.lesswrong.com/posts/abc123/a-post",
        title="A post about oversight",
        author="A. Researcher",
        author_affiliation=None,
        published_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        score=40,
        agreement=None,
        comment_count=4,
        curated=False,
        frontpage=True,
        author_created_at=datetime(2020, 1, 1, tzinfo=UTC),
        text=text,
        markdown=text,
    )


# --- the two end-to-end tests ------------------------------------------------------------


def test_run_daily_end_to_end_with_fixtures(tmp_path):
    clients = _clients()
    day = FixtureClient.day_of(FIX)
    report = run_daily(_inputs(tmp_path, clients))
    assert report.items_fetched > 0 and report.items_selected > 0
    assert set(report.rendered_files) == {"31-today-forums.md", "00-index.md"}
    assert report.published, [g.details for g in report.gates if not g.passed]
    assert (tmp_path / "out" / "31-today-forums.md").read_text().startswith("<!-- aisafety-radar")
    assert (tmp_path / "data" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "run.json").exists()


def test_run_daily_is_idempotent(tmp_path):
    clients = _clients()
    day = FixtureClient.day_of(FIX)
    fetchers = [ForumFetcher("lw", clients["lw"], "lesswrong", 15)]
    first = run_daily(_inputs(tmp_path, clients, fetchers=fetchers))
    second = run_daily(_inputs(tmp_path, clients, fetchers=fetchers))
    assert second.items_fetched == first.items_fetched
    day_dir = tmp_path / "data" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    lines = (day_dir / "items.jsonl").read_text().splitlines()
    assert len(lines) == len(set(lines))
    assert len(lines) == first.items_fetched
    extracted = (day_dir / "extracted.jsonl").read_text().splitlines()
    assert len(extracted) == len(set(extracted))


# --- what the run reports and what it withholds ------------------------------------------


def test_run_daily_reports_the_gates_statuses_and_usage(tmp_path):
    report = run_daily(_inputs(tmp_path, _clients()))
    assert {g.name for g in report.gates} == {
        "provenance",
        "budgets",
        "freshness",
        "injection-scan",
        "faithfulness-judge",
        "cost",
    }
    assert {s.source for s in report.statuses} == {"lesswrong", "alignmentforum", "eaforum"}
    assert all(s.ok for s in report.statuses)
    # CannedLLM never bills, so the run report says so rather than inventing a number.
    assert report.usage.cost_usd == 0.0
    assert report.items_after_floor <= report.items_fetched
    assert report.items_selected <= report.items_after_floor
    assert report.dropped == [] and report.scan_flagged == []


class _FlagsFirstScan(CannedLLM):
    """Canned answers, except the first injection scan comes back flagged."""

    def __init__(self) -> None:
        super().__init__()
        self.flagged = False

    def structured(self, model, system, user, schema, max_tokens=2048):
        out = super().structured(model, system, user, schema, max_tokens)
        if "flagged" in out and not self.flagged:
            self.flagged = True
            return {"flagged": True, "reason": "test injection"}
        return out


def test_scan_flagged_items_are_dropped_named_in_the_report_and_not_rendered(tmp_path):
    report = run_daily(_inputs(tmp_path, _clients(), llm=_FlagsFirstScan()))
    assert len(report.scan_flagged) == 1
    # The item never reaches the pack, so the scan gate passes rather than blocking the run.
    assert report.published, [g.details for g in report.gates if not g.passed]
    flagged = report.scan_flagged[0]
    assert flagged in report.dropped
    rendered = (tmp_path / "out" / "31-today-forums.md").read_text()
    assert flagged.split(":", 1)[1] not in rendered


class _WritesAUrlInTheSummary(CannedLLM):
    """Canned answers, except every summary carries a URL no source ever mentioned."""

    def structured(self, model, system, user, schema, max_tokens=2048):
        out = super().structured(model, system, user, schema, max_tokens)
        if "summary" in out:
            out["summary"] = "See https://evil.example/steal for the details."
        return out


def test_a_blocking_gate_failure_withholds_every_file(tmp_path):
    report = run_daily(_inputs(tmp_path, _clients(), llm=_WritesAUrlInTheSummary()))
    assert not report.published
    provenance = next(g for g in report.gates if g.name == "provenance")
    assert not provenance.passed
    assert any("evil.example/steal" in d for d in provenance.details)
    assert not (tmp_path / "out").exists()
    # The run is still on the record: the archive and the report are written either way.
    day = FixtureClient.day_of(FIX)
    assert (Archive(tmp_path / "data").day_dir(day) / "run.json").exists()


def test_a_blocked_run_leaves_its_items_unseen_so_the_rerun_can_publish_them(tmp_path):
    blocked = run_daily(_inputs(tmp_path, _clients(), llm=_WritesAUrlInTheSummary()))
    assert not blocked.published
    assert not (tmp_path / "data" / "seen-ids.json").exists()
    fixed = run_daily(_inputs(tmp_path, _clients()))
    assert fixed.published, [g.details for g in fixed.gates if not g.passed]
    assert fixed.items_selected == blocked.items_selected
    assert (tmp_path / "out" / "31-today-forums.md").exists()


# --- failure isolation ---------------------------------------------------------------------


class _BrokenWindowFetch(FixtureClient):
    """A client that serves the daily fetch but fails the 14-day window fetch."""

    def posts_since(self, after, view="top", limit=200, require_ai_tag=True):
        if view == "new":
            raise RuntimeError("window fetch is down")
        return super().posts_since(after, view=view, limit=limit, require_ai_tag=require_ai_tag)


def test_one_sites_window_fetch_failing_does_not_kill_the_run(tmp_path, caplog):
    clients = {**_clients(), "lw": _BrokenWindowFetch("lw", FIX)}
    report = run_daily(_inputs(tmp_path, clients))
    assert report.published, [g.details for g in report.gates if not g.passed]
    assert report.items_selected > 0
    assert "window fetch is down" in caplog.text


def test_a_failing_comment_refresh_does_not_kill_the_run(tmp_path, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("comment refresh is down")

    monkeypatch.setattr("pipeline.run.refresh_comments", boom)
    report = run_daily(_inputs(tmp_path, _clients()))
    assert report.published, [g.details for g in report.gates if not g.passed]
    assert report.movers == 0
    assert "comment refresh is down" in caplog.text


def _mover_from_an_older_window_post(record_summary: str = "Contested.") -> Mover:
    """A mover built from an AF post inside the 14-day window but outside today's 24 hours.

    That is exactly the case the split item lists exist for: its URL is known only to the window
    fetch, and its publication date would fail the freshness gate if that gate were given movers.
    """
    day = FixtureClient.day_of(FIX)
    window = FixtureClient("af", FIX).posts_since(day - timedelta(days=14))
    post = max(window, key=lambda p: p.postedAt)
    item = raw_post_to_item("af", post)
    assert item.published_at < Window.for_day(day).start, "fixture post is not an older post"
    extraction = Extraction(
        item_id=item.id,
        summary=record_summary,
        key_quotes=[],
        author_stated_status=None,
        claim_type="exploratory",
        novelty=None,
        discussion_signal=None,
        deadline=None,
        entities=[],
        slugs=["community"],
        importance=3,
    )
    return Mover(
        item_id=item.id,
        title=item.title,
        url=item.url,
        new_comments=12,
        score_before=20,
        score_after=30,
        top_new_comment=None,
        record=ExtractionRecord(extraction=extraction),
    )


def test_a_discussion_movers_url_and_date_clear_the_gates(tmp_path, monkeypatch):
    mover = _mover_from_an_older_window_post()
    monkeypatch.setattr("pipeline.run.refresh_comments", lambda *a, **kw: [mover])
    report = run_daily(_inputs(tmp_path, _clients()))
    # Provenance sees the window posts, so the mover's link traces to a source; freshness does
    # not, so the mover being older than today's window is not a failure.
    assert report.published, [g.details for g in report.gates if not g.passed]
    assert report.movers == 1
    rendered = (tmp_path / "out" / "31-today-forums.md").read_text()
    assert "## Discussion movers" in rendered
    assert mover.url in rendered
    # The budget gate counts posts *and* movers, both of which render as `- **` lines.
    assert rendered.count("\n- **") == report.items_selected + 1


def test_a_movers_url_without_the_window_posts_would_not_pass_provenance(tmp_path, monkeypatch):
    """The other half of that contract: the window items are load-bearing, not incidental."""
    mover = _mover_from_an_older_window_post()
    monkeypatch.setattr("pipeline.run.refresh_comments", lambda *a, **kw: [mover])
    monkeypatch.setattr("pipeline.run._window_posts", lambda clients, day: ([], {}))
    report = run_daily(_inputs(tmp_path, _clients()))
    assert not report.published
    provenance = next(g for g in report.gates if g.name == "provenance")
    assert any(mover.url.removeprefix("https://") in d for d in provenance.details)


def test_a_run_without_clients_still_renders(tmp_path):
    clients = _clients()
    report = run_daily(_inputs(tmp_path, None, fetchers=_fetchers(clients)))
    assert report.published, [g.details for g in report.gates if not g.passed]
    assert report.movers == 0
    assert (tmp_path / "out" / "31-today-forums.md").exists()


# --- the history the floor and the cost gate read -------------------------------------------


def test_source_medians_reads_the_raw_fetcher_counts_from_run_json(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    for offset in range(1, 8):
        archive.write_run(
            day - timedelta(days=offset),
            {"statuses": [{"source": "lesswrong", "items": 10 + offset}]},
        )
    for offset in range(1, 7):
        archive.write_run(
            day - timedelta(days=offset),
            {
                "statuses": [
                    {"source": "lesswrong", "items": 10 + offset},
                    {"source": "eaforum", "items": 5},
                ]
            },
        )
    medians = _source_medians(archive, day)
    # Seven days of history for lesswrong, six for eaforum: too few days is no median at all.
    assert medians == {"lesswrong": 14}


def test_trailing_cost_median_needs_five_days(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    for offset in range(1, 5):
        archive.write_run(day - timedelta(days=offset), {"usage": {"cost_usd": 1.0}})
    assert _trailing_cost_median(archive, day) is None
    archive.write_run(day - timedelta(days=5), {"usage": {"cost_usd": 3.0}})
    assert _trailing_cost_median(archive, day) == 1.0


# --- the fixture client ---------------------------------------------------------------------


def test_fixture_client_parses_the_raw_recorded_payloads():
    posts = FixtureClient("lw", FIX).posts_since(date(2020, 1, 1))
    assert posts and all(isinstance(p, RawPost) for p in posts)
    assert all(p.pageUrl.startswith("https://www.lesswrong.com/") for p in posts)
    comments = FixtureClient("lw", FIX).comments_since(datetime(2020, 1, 1, tzinfo=UTC))
    assert comments and all(isinstance(c, RawComment) for c in comments)
    top = FixtureClient("lw", FIX).top_comments(comments[0].postId)
    assert all(c.postId == comments[0].postId for c in top)


def test_fixture_client_rewrites_the_eaf_mirror_url_to_the_public_one():
    posts = FixtureClient("eaf", FIX).posts_since(date(2020, 1, 1))
    assert posts
    assert all(p.pageUrl.startswith("https://forum.effectivealtruism.org/") for p in posts)
    assert not any("forum-bots" in p.pageUrl for p in posts)


def test_fixture_client_respects_the_after_bound_and_the_limit():
    all_posts = FixtureClient("lw", FIX).posts_since(date(2020, 1, 1))
    newest = max(p.postedAt for p in all_posts).date()
    assert len(FixtureClient("lw", FIX).posts_since(newest)) < len(all_posts)
    assert len(FixtureClient("lw", FIX).posts_since(date(2020, 1, 1), limit=2)) == 2


def test_day_of_puts_the_newest_recorded_post_inside_the_window():
    window = Window.for_day(FixtureClient.day_of(FIX))
    newest = max(
        p.postedAt for s in SITES for p in FixtureClient(s, FIX).posts_since(date(2020, 1, 1))
    )
    assert window.start <= newest < window.end


def test_day_of_needs_recorded_fixtures(tmp_path):
    with pytest.raises(AssertionError):
        FixtureClient.day_of(tmp_path)


# --- the canned model -------------------------------------------------------------------------


def test_canned_llm_answers_every_schema_the_stages_ask_it():
    settings = load_settings()
    llm = CannedLLM()
    item = _item()

    result = triage_item(item, llm, settings)
    assert (result.relevant, result.primary_slug, result.importance) == (True, "community", 3)

    record = extract_verified(item, llm, settings)
    assert not record.degraded, record.notes
    # The summary is copied out of the document, so the substring verifier passes and the
    # numbers and dates in it are in the source by construction.
    assert contains(item.text, record.extraction.summary)
    assert record.extraction.key_quotes and contains(item.text, record.extraction.key_quotes[0])
    assert record.extraction.claim_type == "exploratory"

    assert not scan_record(record, llm, settings).flagged
    assert judge_record(record, item, llm, settings).passed
    assert llm.text(settings.models.haiku, "system", "user") == "canned text"
    assert llm.count_tokens("abcdefgh") == 2
    # Every call is recorded the way FakeLLM records them, so a caller can assert on the wiring:
    # triage, one extraction (verification passed, so no retry) and the scan on Haiku, the judge
    # on Sonnet, then the `text` call above.
    assert [c["model"] for c in llm.calls] == [settings.models.haiku] * 3 + [
        settings.models.sonnet,
        settings.models.haiku,
    ]


def test_canned_llm_summarises_an_empty_document_without_raising():
    record = extract_verified(_item(text=""), CannedLLM(), load_settings())
    assert record.extraction.summary == "No text."
    assert not record.degraded


def test_canned_llm_refuses_a_schema_it_does_not_know():
    with pytest.raises(AssertionError, match="does not know this schema"):
        CannedLLM().structured("m", "s", "u", {"properties": {"nonsense": {}}})
