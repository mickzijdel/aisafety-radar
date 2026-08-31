from datetime import UTC, datetime

from pipeline.config import load_settings
from pipeline.models import Extraction, Usage
from pipeline.qa.gates import (
    blocking_failures,
    gate_budgets,
    gate_cost,
    gate_freshness,
    gate_judge,
    gate_provenance,
    gate_scan,
)
from pipeline.render import PackMeta, build_render_items, render_all, render_forums
from pipeline.stages.comments import Mover
from pipeline.stages.scan import JudgeResult, ScanResult
from pipeline.stages.verify import ExtractionRecord
from tests.test_models import make_item

S = load_settings()
VERSION = "2026.08.30a"
META = PackMeta(
    version=VERSION,
    generated_at=datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
    data_through=datetime(2026, 8, 30, 5, 40, tzinfo=UTC),
)
WINDOW_START = datetime(2026, 8, 29, 5, 40, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 30, 5, 40, tzinfo=UTC)
# The real three-line header, sliced off a real render rather than hand-written, so the prefix the
# freshness gate looks for stays tied to the template that produces it.
HEADER = "\n".join(render_forums(META, [], [], []).splitlines()[:3]) + "\n"


def rec(item_id, importance, degraded=False, summary="s", quotes=()):
    return ExtractionRecord(
        extraction=Extraction(
            item_id=item_id,
            summary=summary,
            key_quotes=list(quotes),
            author_stated_status=None,
            claim_type="exploratory",
            novelty=None,
            discussion_signal=None,
            deadline=None,
            entities=[],
            slugs=["evals"],
            importance=importance,
        ),
        degraded=degraded,
    )


def tokens(text):
    return len(text) // 4


# --- provenance ---------------------------------------------------------------------------


def test_provenance_accepts_item_and_markdown_urls_and_rejects_others():
    item = make_item(markdown="see [x](https://arxiv.org/abs/1234)")
    ok = {"a.md": f"{item.url} and https://arxiv.org/abs/1234 [P{item.id}]"}
    assert gate_provenance(ok, [item]).passed
    bad = {"a.md": "https://evil.example/x"}
    res = gate_provenance(bad, [item])
    assert not res.passed and res.blocking and "evil.example" in res.details[0]
    assert not gate_provenance({"a.md": "[Plw:nope]"}, [item]).passed


def test_provenance_allows_the_packs_own_domain():
    item = make_item()
    own = {
        "a.md": (
            "https://mickzijdel.github.io/aisafety-radar/latest/31-today-forums.md\n"
            "https://mickzijdel.github.io/aisafety-radar/\n"
            "https://mickzijdel.github.io/aisafety-radar\n"
        )
    }
    assert gate_provenance(own, [item]).passed
    # A neighbouring path on the same host is not the pack.
    other = {"a.md": "https://mickzijdel.github.io/something-else/x"}
    assert not gate_provenance(other, [item]).passed


def test_provenance_ignores_trailing_punctuation_on_both_sides():
    item = make_item(markdown="link: https://arxiv.org/abs/1234.")
    assert gate_provenance({"a.md": "see https://arxiv.org/abs/1234."}, [item]).passed
    assert gate_provenance({"a.md": f"({item.url}),"}, [item]).passed
    # A link inside a markdown code span keeps the closing backtick when the URL is scanned out.
    assert gate_provenance({"a.md": f"`{item.url}`,"}, [item]).passed
    # Stripping punctuation only shortens the candidate; it never launders a foreign host.
    assert not gate_provenance({"a.md": "`https://evil.example/x`."}, [item]).passed


def test_provenance_catches_a_model_written_url_in_rendered_prose():
    # The renderer flattens and escapes model prose, but a URL inside it passes through verbatim.
    # This gate is the thing standing between a hallucinated link and a published one.
    item = make_item(id="lw:1", markdown="no links in the source")
    record = rec("lw:1", 3, summary="Contested. Full write-up at https://evil.example/x.")
    text = render_forums(META, build_render_items([item], [record]), [], [])
    assert "https://evil.example/x" in text, "precondition: the URL survives rendering"
    res = gate_provenance({"31-today-forums.md": text}, [item])
    assert not res.passed and res.blocking
    assert "evil.example" in res.details[0]


def test_provenance_needs_an_item_for_every_rendered_mover():
    # Movers are rendered from ledger entries, not Items, so the caller has to hand the gate an
    # Item for each one; without it the mover's own link reads as unsourced.
    post = make_item(id="lw:1")
    mover_item = make_item(id="lw:9", url="https://www.lesswrong.com/posts/9/y")
    movers = [Mover("lw:9", "Older post", mover_item.url, 12, 40, 60, None, rec("lw:9", 3))]
    text = render_forums(META, build_render_items([post], [rec("lw:1", 3)]), movers, [])
    assert not gate_provenance({"31-today-forums.md": text}, [post]).passed
    assert gate_provenance({"31-today-forums.md": text}, [post, mover_item]).passed


def test_provenance_reports_every_offending_file_and_reference():
    item = make_item()
    res = gate_provenance(
        {"a.md": "https://evil.example/x", "b.md": "[Peaf:missing]"},
        [item],
    )
    assert not res.passed and len(res.details) == 2
    assert any("a.md" in d for d in res.details) and any("b.md" in d for d in res.details)


# --- budgets ------------------------------------------------------------------------------


def test_budgets_and_counts():
    text = HEADER + "- **one**\n- **two**\n"
    res = gate_budgets({"31-today-forums.md": text}, S, lambda s: 10, {"31-today-forums.md": 2})
    assert res.passed
    assert not gate_budgets(
        {"31-today-forums.md": text}, S, lambda s: 10, {"31-today-forums.md": 3}
    ).passed
    assert not gate_budgets(
        {"31-today-forums.md": text}, S, lambda s: 10_000, {"31-today-forums.md": 2}
    ).passed


def test_budgets_gate_is_blocking_and_names_the_file():
    text = HEADER + "- **one**\n"
    res = gate_budgets({"31-today-forums.md": text}, S, lambda s: 10_000, {})
    assert not res.passed and res.blocking
    assert "31-today-forums.md" in res.details[0] and "9000" in res.details[0]


def test_budgets_skips_files_with_no_budget_and_no_expected_count():
    assert gate_budgets({"99-unknown.md": "x" * 10}, S, lambda s: 10_000_000, {}).passed


def test_budgets_counts_movers_as_items():
    # The movers section emits `- **` lines too, so the expected count the caller passes for the
    # forums file is posts + movers, not posts.
    post = make_item(id="lw:1", published_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    movers = [
        Mover(
            "lw:9",
            "Older post",
            "https://www.lesswrong.com/posts/9/y",
            12,
            40,
            60,
            None,
            rec("lw:9", 3),
        )
    ]
    text = render_forums(META, build_render_items([post], [rec("lw:1", 3)]), movers, [])
    rendered = {"31-today-forums.md": text}
    assert gate_budgets(rendered, S, tokens, {"31-today-forums.md": 2}).passed
    assert not gate_budgets(rendered, S, tokens, {"31-today-forums.md": 1}).passed


# --- freshness ----------------------------------------------------------------------------


def test_freshness():
    start, end = WINDOW_START, WINDOW_END
    inside = make_item(published_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    assert gate_freshness({"a.md": HEADER}, [inside], start, end, VERSION).passed
    assert not gate_freshness({"a.md": "no header"}, [inside], start, end, VERSION).passed
    old = make_item(published_at=datetime(2026, 8, 1, tzinfo=UTC))
    assert not gate_freshness({"a.md": HEADER}, [old], start, end, VERSION).passed


def test_freshness_rejects_a_header_for_a_different_version():
    inside = make_item(published_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    res = gate_freshness({"a.md": HEADER}, [inside], WINDOW_START, WINDOW_END, "2026.08.30b")
    assert not res.passed and res.blocking and "a.md" in res.details[0]


def test_freshness_window_is_half_open():
    at_start = make_item(id="lw:s", published_at=WINDOW_START)
    at_end = make_item(id="lw:e", published_at=WINDOW_END)
    assert gate_freshness({"a.md": HEADER}, [at_start], WINDOW_START, WINDOW_END, VERSION).passed
    res = gate_freshness({"a.md": HEADER}, [at_end], WINDOW_START, WINDOW_END, VERSION)
    assert not res.passed and "lw:e" in res.details[0]


# --- scan and judge -----------------------------------------------------------------------


def test_scan_and_judge_gates():
    scans = {"lw:1": ScanResult(True, "instruction"), "lw:2": ScanResult(False, None)}
    assert gate_scan(scans, {"lw:2"}).passed and not gate_scan(scans, {"lw:1"}).passed
    judges = {"lw:1": JudgeResult(False, True, True, False, "untraceable")}
    assert not gate_judge(judges, {"lw:1": rec("lw:1", 5)}).passed
    assert gate_judge(judges, {"lw:1": rec("lw:1", 5, degraded=True)}).passed
    assert gate_judge(judges, {"lw:1": rec("lw:1", 2)}).passed


def test_scan_gate_is_blocking_and_reports_the_reason():
    res = gate_scan({"lw:1": ScanResult(True, "ignore previous instructions")}, {"lw:1"})
    assert not res.passed and res.blocking
    assert "lw:1" in res.details[0] and "ignore previous instructions" in res.details[0]


def test_judge_gate_passes_a_passing_verdict_and_skips_unknown_ids():
    passing = {"lw:1": JudgeResult(True, True, True, True, "")}
    assert gate_judge(passing, {"lw:1": rec("lw:1", 5)}).passed
    failing = {"lw:1": JudgeResult(False, True, True, False, "untraceable")}
    # A verdict for an item that never made it into a record cannot be acted on here.
    assert gate_judge(failing, {}).passed


def test_judge_gate_is_blocking_and_names_the_item():
    judges = {"lw:1": JudgeResult(False, True, True, False, "untraceable")}
    res = gate_judge(judges, {"lw:1": rec("lw:1", 4)})
    assert not res.passed and res.blocking
    assert "lw:1" in res.details[0] and "untraceable" in res.details[0]


# --- cost and aggregation -----------------------------------------------------------------


def test_cost_gate_non_blocking():
    res = gate_cost(Usage(cost_usd=3.0), trailing_median=1.0, multiplier=2.0)
    assert not res.passed and not res.blocking
    assert gate_cost(Usage(cost_usd=3.0), None, 2.0).passed
    assert blocking_failures([res]) == []


def test_cost_gate_at_exactly_the_multiple_passes():
    assert gate_cost(Usage(cost_usd=2.0), 1.0, 2.0).passed
    assert not gate_cost(Usage(cost_usd=2.01), 1.0, 2.0).passed


def test_blocking_failures_keeps_only_blocking_failures_in_order():
    ok = gate_scan({}, set())
    cost = gate_cost(Usage(cost_usd=3.0), 1.0, 2.0)
    bad_scan = gate_scan({"lw:1": ScanResult(True, "x")}, {"lw:1"})
    bad_fresh = gate_freshness({"a.md": "no header"}, [], WINDOW_START, WINDOW_END, VERSION)
    assert blocking_failures([ok, cost, bad_scan, bad_fresh]) == [bad_scan, bad_fresh]
    assert blocking_failures([]) == []


# --- the gates against a real rendered pack -----------------------------------------------


def test_a_real_rendered_pack_clears_every_rendering_gate():
    post = make_item(id="lw:1", published_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    mover_item = make_item(
        id="lw:9",
        url="https://www.lesswrong.com/posts/9/y",
        published_at=datetime(2026, 8, 29, 13, tzinfo=UTC),
    )
    movers = [Mover("lw:9", "Older post", mover_item.url, 12, 40, 60, None, rec("lw:9", 3))]
    rendered = render_all(META, build_render_items([post], [rec("lw:1", 3)]), movers, [], tokens)
    items = [post, mover_item]
    assert set(rendered) == {"31-today-forums.md", "00-index.md"}
    assert gate_provenance(rendered, items).passed
    assert gate_freshness(rendered, items, WINDOW_START, WINDOW_END, VERSION).passed
    # Only the forums file gets an expected count: the index's `- **` lines are recipes, not items.
    assert gate_budgets(rendered, S, tokens, {"31-today-forums.md": 2}).passed
