import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pipeline.models import Comment, Extraction, SourceStatus
from pipeline.render import (
    APPENDIX_PURPOSE,
    PackMeta,
    build_render_items,
    render_all,
    render_forums,
    render_index,
)
from pipeline.stages.comments import Mover
from pipeline.stages.verify import ExtractionRecord
from tests.test_models import make_item

GOLDEN = Path(__file__).parent / "golden"
SPEC = (
    Path(__file__).parents[1]
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-08-30-aisafety-radar-design.md"
)
META = PackMeta(
    version="2026.08.30a",
    generated_at=datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
    data_through=datetime(2026, 8, 30, 5, 40, tzinfo=UTC),
)


def rec(item_id, importance=3, degraded=False, quotes=("a quote",), signal=None):
    e = Extraction(
        item_id=item_id,
        summary="Claims X with a 45% drop.",
        key_quotes=list(quotes),
        author_stated_status="Epistemic status: exploratory",
        claim_type="exploratory",
        novelty="First test of X.",
        discussion_signal=signal,
        deadline=None,
        entities=[],
        slugs=["evals"],
        importance=importance,
    )
    return ExtractionRecord(extraction=e, degraded=degraded)


def fixture():
    items = [
        make_item(id="lw:1", title="Post one", score=45, agreement=12),
        make_item(
            id="eaf:2",
            site="eaf",
            source="eaforum",
            title="Post two",
            score=80,
            url="https://forum.effectivealtruism.org/posts/2/x",
            agreement=None,
        ),
    ]
    records = [
        rec("lw:1", importance=5, signal="Contested; Bob (83 karma) disagrees."),
        rec("eaf:2", importance=2, degraded=True, quotes=()),
    ]
    movers = [
        Mover(
            "lw:9",
            "Older post",
            "https://www.lesswrong.com/posts/9/y",
            12,
            40,
            60,
            None,
            rec("lw:9"),
        )
    ]
    return items, records, movers


def check_golden(name, text):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), (
        f"{name} differs from golden; run with UPDATE_GOLDEN=1 to accept"
    )


def test_render_items_order_and_fields():
    items, records, _ = fixture()
    ri = build_render_items(items, records)
    assert [r.title for r in ri] == ["Post one", "Post two"]
    assert ri[0].agreement_line == "agree 12" and ri[1].agreement_line is None
    assert ri[0].site_label == "LW" and ri[1].site_label == "EAF"


def test_render_items_order_by_importance_then_score():
    items = [
        make_item(id="lw:1", title="Low importance, high score", score=900),
        make_item(id="lw:2", title="High importance, low score", score=1),
        make_item(id="lw:3", title="High importance, mid score", score=50),
    ]
    records = [rec("lw:1", importance=1), rec("lw:2", importance=5), rec("lw:3", importance=5)]
    assert [r.title for r in build_render_items(items, records)] == [
        "High importance, mid score",
        "High importance, low score",
        "Low importance, high score",
    ]


def test_items_without_an_extraction_are_dropped():
    items = [make_item(id="lw:1", title="Kept"), make_item(id="lw:2", title="Never extracted")]
    assert [r.title for r in build_render_items(items, [rec("lw:1")])] == ["Kept"]


def test_zero_agreement_is_not_rendered_as_signal():
    # ForumMagnum's extendedScore.agreement is a *net* score: 0 means no agreement votes were
    # cast (or that they cancelled), not "the readers are neutral", so it must not be rendered.
    items = [make_item(id="lw:1", title="Nobody voted", agreement=0)]
    ri = build_render_items(items, [rec("lw:1")])
    assert ri[0].agreement_line is None
    text = render_forums(META, ri, [], [])
    assert "· agree" not in text
    assert "(LW · 2026-08-29 · 45 karma · 3 comments)" in text


def test_negative_agreement_is_rendered():
    items = [make_item(id="lw:1", title="Net disagreement", agreement=-7)]
    ri = build_render_items(items, [rec("lw:1")])
    assert ri[0].agreement_line == "agree -7"


def test_missing_score_omits_the_karma_segment():
    items = [make_item(id="lw:1", title="Unscored", score=None)]
    text = render_forums(META, build_render_items(items, [rec("lw:1")]), [], [])
    assert "(LW · 2026-08-29 · 3 comments)" in text
    assert "karma" not in text.split("## Posts")[1]


def test_prose_fields_are_collapsed_onto_one_line():
    items = [make_item(id="lw:1", title="Wrapped\nsource  title")]
    r = build_render_items(items, [rec("lw:1", quotes=("a\nwrapped quote",))])[0]
    assert r.title == "Wrapped source title" and r.quotes == ["a wrapped quote"]


def test_forums_golden():
    items, records, movers = fixture()
    text = render_forums(META, build_render_items(items, records), movers, [])
    assert text.startswith(
        "<!-- aisafety-radar · version 2026.08.30a · generated 2026-08-30T06:14Z"
    )
    assert "https://www.lesswrong.com/posts/abc123/x" in text
    assert "summary withheld" in text and "Discussion: Contested; Bob (83 karma) disagrees." in text
    assert "karma 40 -> 60" in text
    check_golden("31-today-forums.md", text)


def test_header_is_a_single_html_comment_of_three_lines():
    items, records, movers = fixture()
    head = render_forums(META, build_render_items(items, records), movers, []).split("-->")[0]
    assert head.count("<!--") == 1
    assert len(head.splitlines()) == 3
    assert "layer: daily · latest: " in head
    assert "data through 2026-08-30T05:40Z" in head
    assert "Summaries are machine-generated." in head


def test_failed_sources_are_named_in_the_lead_paragraph():
    items, records, movers = fixture()
    statuses = [
        SourceStatus(source="lesswrong", category="forums", ok=True, items=2),
        SourceStatus(source="eaforum", category="forums", ok=False, items=0, error="timeout"),
    ]
    text = render_forums(META, build_render_items(items, records), movers, statuses)
    assert "ordered by importance. Source unavailable today: eaforum." in text
    assert "lesswrong." not in text.split("## Posts")[0]


def test_no_posts_renders_a_sentence_rather_than_an_empty_section():
    text = render_forums(META, [], [], [])
    assert "0 posts from LessWrong" in text
    assert "No posts from these forums cleared the bar today." in text
    assert "## Discussion movers" not in text


def test_one_post_is_singular():
    items = [make_item(id="lw:1", comment_count=1)]
    text = render_forums(META, build_render_items(items, [rec("lw:1")]), [], [])
    assert "1 post from LessWrong" in text
    assert "· 1 comment)" in text


def test_karma_only_mover_omits_the_comment_segment():
    _, _, _ = fixture()
    movers = [
        Mover(
            "lw:9",
            "Karma only",
            "https://www.lesswrong.com/posts/9/y",
            0,
            40,
            60,
            None,
            rec("lw:9"),
        ),
        Mover(
            "lw:8",
            "Retracted votes",
            "https://www.lesswrong.com/posts/8/y",
            -2,
            9,
            4,
            None,
            rec("lw:8"),
        ),
    ]
    text = render_forums(META, [], movers, [])
    assert "- **Karma only** · karma 40 -> 60 · https://www.lesswrong.com/posts/9/y" in text
    assert "- **Retracted votes** · karma 9 -> 4 · https://www.lesswrong.com/posts/8/y" in text
    assert "comments" not in text.split("## Discussion movers")[1]


def test_mover_renders_its_new_top_comment():
    comment = Comment(
        id="lw:c1",
        post_id="lw:9",
        site="lw",
        author="Bob",
        author_created_at=None,
        posted_at=datetime(2026, 8, 30, tzinfo=UTC),
        score=83,
        agreement=None,
        parent_id=None,
        text="body",
    )
    movers = [
        Mover(
            "lw:9",
            "Older post",
            "https://www.lesswrong.com/posts/9/y",
            12,
            None,
            None,
            comment,
            rec("lw:9"),
        )
    ]
    text = render_forums(META, [], movers, [])
    assert "- **Older post** · +12 comments · https://www.lesswrong.com/posts/9/y" in text
    assert "  New top comment by Bob (83 karma)." in text


def test_degraded_item_withholds_the_summary_but_keeps_metadata_and_quotes():
    items = [make_item(id="lw:1", title="Unverifiable")]
    text = render_forums(META, build_render_items(items, [rec("lw:1", degraded=True)]), [], [])
    assert "- **Unverifiable** · A (LW · 2026-08-29 · 45 karma · 3 comments)" in text
    assert "  (summary withheld: could not be verified against the source)" in text
    assert "Claims X with a 45% drop." not in text
    assert "Claim type:" not in text
    assert '  > "a quote"' in text


def test_appendix_purpose_covers_every_file_in_spec_4_1():
    layout = SPEC.read_text().split("### 4.1 Layout")[1].split("### 4.2")[0]
    names = set(re.findall(r"^\s{4}(\d\d-[a-z0-9-]+\.md)\s", layout, re.MULTILINE))
    assert names, "could not read the appendix list out of spec 4.1"
    assert names == set(APPENDIX_PURPOSE)


def test_index_golden_and_recipes():
    items, records, movers = fixture()
    forums = render_forums(META, build_render_items(items, records), movers, [])
    text = render_index(META, {"31-today-forums.md": forums}, lambda s: len(s) // 4)
    assert "| `31-today-forums.md` |" in text and "not yet published" in text
    check_golden("00-index.md", text)


def test_index_lists_every_appendix_but_itself():
    text = render_index(META, {}, lambda s: len(s) // 4)
    for name in APPENDIX_PURPOSE:
        if name == "00-index.md":
            continue
        assert f"| `{name}` |" in text, name
    assert "| `00-index.md` |" not in text


def test_index_counts_tokens_with_the_injected_counter():
    text = render_index(META, {"31-today-forums.md": "x" * 400}, lambda s: len(s) // 4)
    assert (
        "| `31-today-forums.md` | daily: LW / AF / EAF posts and discussion movers | 100 |" in text
    )
    assert "`31-today-forums.md` (about 100 tokens)" in text


def test_index_recipe_with_nothing_published_states_only_what_is_missing():
    text = render_index(META, {}, lambda s: len(s) // 4)
    assert "- **this week**: not yet published: `21-this-week.md`" in text
    assert "about 0 tokens" not in text


def test_render_all_returns_both_files():
    items, records, movers = fixture()
    out = render_all(META, build_render_items(items, records), movers, [], lambda s: len(s) // 4)
    assert set(out) == {"31-today-forums.md", "00-index.md"}


def test_output_has_no_em_dashes_and_ends_in_one_newline():
    items, records, movers = fixture()
    for name, text in render_all(
        META, build_render_items(items, records), movers, [], lambda s: len(s) // 4
    ).items():
        assert "—" not in text and "–" not in text, name
        assert text.endswith("\n") and not text.endswith("\n\n"), name
        assert "\n\n\n" not in text, name
        for line in text.splitlines():
            assert line == line.rstrip(), f"{name}: trailing whitespace in {line!r}"
