from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.archive import Archive
from pipeline.config import load_settings
from pipeline.models import Extraction, Usage
from pipeline.qa.gates import (
    OWN_BASE,
    blocking_failures,
    gate_budgets,
    gate_cost,
    gate_freshness,
    gate_judge,
    gate_provenance,
    gate_scan,
    gate_source_minimums,
)
from pipeline.render import PAGES_BASE, PackMeta, build_render_items, render_all, render_forums
from pipeline.stages.comments import Mover
from pipeline.stages.scan import JudgeResult, ScanResult
from pipeline.stages.verify import ExtractionRecord
from tests.test_models import make_comment, make_item

S = load_settings()
VERSION = "2026.08.30a"
META = PackMeta(
    version=VERSION,
    generated_at=datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
    data_from=datetime(2026, 8, 29, 4, 40, tzinfo=UTC),
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


def test_provenance_allows_a_url_in_a_movers_top_comment_author():
    # A mover's new top comment renders its commenter's display name, and a display name is
    # fetched source text exactly as a post's author is -- but it reaches the pack through the
    # mover path, which carries no `Item`. The caller hands those names to the gate as extra
    # source text, or a forum user called `www.example.org/me` withholds the whole pack.
    post = make_item(id="lw:1")
    mover_item = make_item(id="lw:9", url="https://www.lesswrong.com/posts/9/y")
    named = make_comment(author="read www.example.org/me for more")
    movers = [Mover("lw:9", "Older post", mover_item.url, 12, 40, 60, named, rec("lw:9", 3))]
    text = render_forums(META, build_render_items([post], [rec("lw:1", 3)]), movers, [])
    assert "www.example.org/me" in text, "precondition: the commenter's name reaches the pack"
    items = [post, mover_item]
    assert not gate_provenance({"31-today-forums.md": text}, items).passed
    res = gate_provenance({"31-today-forums.md": text}, items, source_text=[named.author])
    assert res.passed, res.details


def test_provenance_extra_source_text_does_not_launder_an_unrelated_link():
    # The widening is exactly as wide as the text handed in: a commenter's name vouches for the
    # link *in that name*, and for nothing else.
    item = make_item(markdown="no links")
    res = gate_provenance(
        {"a.md": "https://evil.example/x"}, [item], source_text=["www.example.org/me"]
    )
    assert not res.passed and "evil.example" in res.details[0]


# A URL only has to be *readable* as a link to do the damage this gate exists to stop: the digest
# is loaded into other agents' context windows, and an agent asked to "check the source" will
# follow a bare host/path as readily as a fully-schemed one. Requiring `https://` let four shapes
# of the same hostile link straight through.
@pytest.mark.parametrize(
    ("label", "written"),
    [
        ("control, fully schemed", "https://evil.example/x"),
        ("uppercase scheme", "HTTPS://EVIL.EXAMPLE/x"),
        ("mixed-case scheme", "HtTpS://evil.example/x"),
        ("scheme-less www", "www.evil.example/x"),
        ("bare host and path", "evil.example/x"),
        # The bare-host pattern needs a path to tell a host from a word, so a pathless link is
        # only caught by the scheme and www patterns. Both therefore have to carry their weight.
        ("uppercase scheme, no path", "HTTPS://EVIL.EXAMPLE"),
        ("www host, no path", "www.evil.example"),
    ],
)
def test_provenance_flags_every_shape_of_an_unsourced_link(label, written):
    item = make_item(markdown="no links in the source")
    res = gate_provenance({"a.md": f"see {written} for details"}, [item])
    assert not res.passed and res.blocking, label
    assert "evil.example" in res.details[0].lower(), label


def test_provenance_accepts_an_item_url_written_without_its_scheme():
    # The flip side of scanning scheme-less tokens: a legitimate link written bare must still
    # match the item it came from, so item URLs are indexed without their scheme too.
    item = make_item()  # https://www.lesswrong.com/posts/abc123/x
    assert gate_provenance({"a.md": "www.lesswrong.com/posts/abc123/x"}, [item]).passed
    assert gate_provenance({"a.md": "at www.lesswrong.com/posts/abc123/x."}, [item]).passed
    # And a scheme-less link the source never carried is still caught.
    assert not gate_provenance({"a.md": "www.lesswrong.com/posts/other/x"}, [item]).passed


def test_provenance_reports_the_offending_url_as_the_file_spells_it():
    # The comparison folds case; the report must not, or the reader of a blocked run is sent to
    # look up a path that may not exist in the case the detail prints.
    item = make_item(markdown="no links")
    res = gate_provenance({"a.md": "**https://Evil.Example/Steal**."}, [item])
    assert res.details == ["a.md: URL not in any source: Evil.Example/Steal"]


def test_provenance_reports_one_detail_per_offending_url():
    # The scheme-ful and bare patterns both match a fully-written URL; that is one problem.
    item = make_item()
    res = gate_provenance({"a.md": "https://evil.example/x"}, [item])
    assert len(res.details) == 1


@pytest.mark.parametrize(
    "prose",
    [
        "the GPT-4.5/turbo results",
        "rated 4.5/5 by reviewers",
        "released as v1.2/beta",
        "see pipeline/qa/gates.py and docs/spec.md",
        "a 50/50 split at 30 km/h",
        "dated 2026.08.30/rev2",
    ],
)
def test_provenance_does_not_read_ordinary_prose_as_a_link(prose):
    # A false positive here blocks the whole pack, so the bare-host pattern requires a
    # TLD-shaped final label before the slash; decimals and file paths are not links.
    item = make_item(id="lw:1", markdown="no links")
    record = rec("lw:1", 3, summary=prose)
    text = render_forums(META, build_render_items([item], [record]), [], [])
    assert gate_provenance({"31-today-forums.md": text}, [item]).passed


def test_provenance_finds_no_links_to_object_to_in_the_goldens():
    # The zero-false-positive proof, against the bytes the renderer actually committed.
    golden = Path(__file__).parent / "golden"
    rendered = {p.name: p.read_text() for p in sorted(golden.glob("*.md"))}
    assert set(rendered) == {"00-index.md", "31-today-forums.md"}, "goldens moved"
    items = [
        make_item(id="lw:1", url="https://www.lesswrong.com/posts/abc123/x"),
        make_item(id="eaf:2", url="https://forum.effectivealtruism.org/posts/2/x"),
        make_item(id="lw:9", url="https://www.lesswrong.com/posts/9/y"),
    ]
    res = gate_provenance(rendered, items)
    assert res.passed, res.details


# Four shapes of a *sourced* link the gate used to reject. Each one blocks the whole pack, which
# is the worse of the two failures this gate can have: a false positive withholds a correct digest
# for a day, and there is no way for the run to recover from it on its own.
@pytest.mark.parametrize(
    ("label", "source", "written"),
    [
        ("host case differs from the source", "see WWW.Example.com/Paper", "www.example.com/Paper"),
        ("written in upper case", "see www.example.com/Paper", "WWW.EXAMPLE.COM/Paper"),
        ("scheme dropped, case kept", "see https://Host.Example/X", "host.example/X"),
        ("wrapped in bold at line end", "see https://host.example/x", "**host.example/x**"),
        ("wrapped in single emphasis", "see https://host.example/x", "*host.example/x*"),
    ],
)
def test_provenance_accepts_a_sourced_link_however_it_is_written(label, source, written):
    item = make_item(markdown=source)
    res = gate_provenance({"a.md": written}, [item])
    assert res.passed, (label, res.details)


@pytest.mark.parametrize(
    ("label", "written"),
    [
        ("bold", "**evil.example/x**"),
        ("upper case", "WWW.EVIL.EXAMPLE/x"),
        ("emphasis", "*https://evil.example/x*"),
    ],
)
def test_provenance_still_flags_an_invented_link_in_those_same_shapes(label, written):
    item = make_item(markdown="see https://host.example/x")
    res = gate_provenance({"a.md": written}, [item])
    assert not res.passed and res.blocking, label
    assert "evil.example" in res.details[0].lower(), label


@pytest.mark.parametrize(
    ("label", "field"),
    [("title", "title"), ("author", "author"), ("affiliation", "author_affiliation")],
)
def test_provenance_allows_a_link_the_fetched_metadata_itself_carries(label, field):
    # Spec 6.8 gate 1 allows what "appears verbatim in fetched source text", and a post's title,
    # author name and affiliation are fetched source text: the renderer copies them onto the
    # metadata line, so a forum user whose display name is a URL would otherwise block the pack.
    item = make_item(**{field: "read www.example.org/launch for more"})
    rendered = render_forums(META, build_render_items([item], [rec(item.id, 3)]), [], [])
    assert "www.example.org/launch" in rendered, "precondition: the field reaches the pack"
    res = gate_provenance({"31-today-forums.md": rendered}, [item])
    assert res.passed, (label, res.details)


def test_provenance_reports_every_offending_file_and_reference():
    item = make_item()
    res = gate_provenance(
        {"a.md": "https://evil.example/x", "b.md": "[Peaf:missing]"},
        [item],
    )
    assert not res.passed and len(res.details) == 2
    assert any("a.md" in d for d in res.details) and any("b.md" in d for d in res.details)


@pytest.mark.parametrize(
    "token",
    [
        "[Plw:nope]",  # well-formed shape, unknown id
        "[P123]",  # no site prefix at all
        "[PLW:abc]",  # right shape, wrong case
        "[Plw:]",  # site prefix, no local id
    ],
)
def test_provenance_flags_a_citation_that_does_not_resolve(token):
    item = make_item()  # lw:abc123
    res = gate_provenance({"a.md": f"as shown {token} in the source"}, [item])
    assert not res.passed and res.blocking


def test_provenance_does_not_read_a_bracketed_phrase_as_a_citation():
    # A citation is one token; `[Papers we liked]` is a sentence in brackets, and reading it as a
    # broken citation would block the pack on ordinary prose.
    item = make_item()
    assert gate_provenance({"a.md": "the [Papers we liked] section"}, [item]).passed


def test_provenance_resolves_a_well_formed_citation():
    item = make_item()
    assert gate_provenance({"a.md": f"[P{item.id}]"}, [item]).passed


@pytest.mark.parametrize(
    "phrase",
    [
        "[Python:3.12]",  # the shape that blocked a pack: a colon in ordinary prose
        "[Pytorch:2.4]",
        "[Priority:high]",
        "[Pzz:abc123]",  # a site the pipeline does not fetch cannot be one of its items
        "[Part 2: the sequel]",
    ],
)
def test_provenance_does_not_read_a_colon_in_prose_as_a_citation(phrase):
    # A citation is `[P<site>:<local-id>]` for a site this pipeline actually fetches, or a bare
    # number. Anything else in brackets is prose, and reading `[Python:3.12]` as a broken citation
    # withheld a whole correct pack. The narrowing is deliberate: an unresolvable `[Pzz:x]` renders
    # no link and misleads nobody, while a false positive here costs the day's publication.
    item = make_item()
    assert gate_provenance({"a.md": f"built on {phrase} today"}, [item]).passed


@pytest.mark.parametrize("token", ["[Plw]", "[Paf]", "[Peaf]", "[PLW]", "[Peaf.]"])
def test_provenance_does_not_read_a_colon_less_site_prefix_as_a_citation(token):
    # `[Plw]`, `[Paf]` and `[Peaf]` are the site names with the citation's `P` in front, and a
    # citation is `[P<site>:<local-id>]`. Without the colon there is no local id to resolve, so
    # the token is prose -- and reading it as a broken citation would withhold the whole pack over
    # a bracketed word, the same false positive `[Python:3.12]` produced from the other direction.
    item = make_item()
    assert gate_provenance({"a.md": f"the {token} forum"}, [item]).passed


@pytest.mark.parametrize("word", ["[PDF]", "[Paper]", "[Preprint]", "[Post](url)"])
def test_provenance_does_not_read_bracketed_words_as_citations(word):
    # Widening the citation pattern must not turn ordinary bracketed prose into a blocking
    # failure. An item id is `<site>:<local-id>`, so a colon (or an all-digit body) is what marks
    # a token as a citation attempt rather than a word.
    item = make_item()
    assert gate_provenance({"a.md": f"the {word} version"}, [item]).passed


def test_the_gate_and_the_renderer_share_one_pages_base():
    assert OWN_BASE == f"{PAGES_BASE}/"
    assert META.latest_base.startswith(PAGES_BASE)


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


def test_freshness_does_not_accept_a_version_that_merely_starts_with_the_expected_one():
    # The version is a prefix of the next day's suffixed version, so the match has to consume the
    # separator too or `2026.08.30ab` passes as `2026.08.30a`.
    inside = make_item(published_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    longer = "\n".join(
        render_forums(
            PackMeta(
                version="2026.08.30ab",
                generated_at=META.generated_at,
                data_from=META.data_from,
                data_through=META.data_through,
            ),
            [],
            [],
            [],
        ).splitlines()[:3]
    )
    assert not gate_freshness({"a.md": longer}, [inside], WINDOW_START, WINDOW_END, VERSION).passed
    assert gate_freshness(
        {"a.md": longer}, [inside], WINDOW_START, WINDOW_END, "2026.08.30ab"
    ).passed


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


# --- the 7-day minimums -------------------------------------------------------------------


class _Fetcher:
    """Enough of the `Fetcher` protocol for the minimums gate: a name, a category, a floor."""

    def __init__(self, name, category, min_items_7d):
        self.name = name
        self.category = category
        self.min_items_7d = min_items_7d

    def fetch(self, window):  # pragma: no cover - the gate never fetches
        raise NotImplementedError


FETCHERS = [
    _Fetcher("lesswrong", "forums", 15),
    _Fetcher("alignmentforum", "forums", 3),
    _Fetcher("eaforum", "forums", 5),
]


def _history(archive, day, days, per_day):
    """Write `days` run reports before `day`, each recording `per_day` items per source."""
    for offset in range(1, days + 1):
        archive.write_run(
            day - timedelta(days=offset),
            {
                "statuses": [
                    {"source": name, "category": "forums", "ok": True, "items": items}
                    for name, items in per_day.items()
                ]
            },
        )


def test_source_minimums_passes_when_every_source_clears_its_floor(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    _history(archive, day, 7, {"lesswrong": 4, "alignmentforum": 1, "eaforum": 2})
    res = gate_source_minimums(archive, day, FETCHERS)
    assert res.passed, res.details
    # Never blocking: a quiet week is a thing to look at, not a reason to withhold a correct pack.
    assert not res.blocking


def test_source_minimums_names_a_source_below_its_floor(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    # 7 days x 1 post = 7 for LessWrong, under its minimum of 15; the other two clear theirs.
    _history(archive, day, 7, {"lesswrong": 1, "alignmentforum": 1, "eaforum": 2})
    res = gate_source_minimums(archive, day, FETCHERS)
    assert not res.passed and not res.blocking
    assert any("lesswrong" in d and "7" in d and "15" in d for d in res.details), res.details
    assert not any("alignmentforum" in d for d in res.details)


def test_source_minimums_rolls_the_categorys_sources_up_into_one_row(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    # A whole layer of the pack going quiet: LessWrong is down to a post a day and the other two
    # produced nothing all week. The per-source rows say which sources; the category row says how
    # far the area as a whole fell short, which is the number spec 6.1 asks about.
    _history(archive, day, 7, {"lesswrong": 1, "alignmentforum": 0, "eaforum": 0})
    res = gate_source_minimums(archive, day, FETCHERS)
    assert not res.passed
    category = [d for d in res.details if d.startswith("category forums")]
    assert len(category) == 1, res.details
    # 7 + 0 + 0 items against 15 + 3 + 5 minimums.
    assert "7 items" in category[0] and "minimum 23" in category[0], category[0]


def test_source_minimums_reports_no_category_row_while_the_area_holds_up(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    # One source short, the other two well over: the area as a whole is fine, so only the source
    # that is actually short is named. The category row is a rollup of the same counts, so it can
    # never fire on its own -- it fires when the shortfall is big enough to sink the total.
    _history(archive, day, 7, {"lesswrong": 1, "alignmentforum": 3, "eaforum": 3})
    res = gate_source_minimums(archive, day, FETCHERS)
    assert res.details == ["lesswrong: 7 items in 7 days, below the minimum 15"]


def test_source_minimums_says_nothing_until_there_are_seven_runs_to_read(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    _history(archive, day, 6, {"lesswrong": 0, "alignmentforum": 0, "eaforum": 0})
    res = gate_source_minimums(archive, day, FETCHERS)
    # Six days of nothing at all, and the gate still passes: a week's floor cannot be judged on
    # less than a week, and a pipeline started on Wednesday must not alert every day until Sunday.
    assert res.passed
    assert res.details == ["6 of the last 7 runs recorded: too little history to judge a week"]
    _history(archive, day, 7, {"lesswrong": 0, "alignmentforum": 0, "eaforum": 0})
    assert not gate_source_minimums(archive, day, FETCHERS).passed


def test_source_minimums_reads_a_source_missing_from_the_reports_as_zero(tmp_path):
    archive = Archive(tmp_path)
    day = date(2026, 8, 31)
    # A source that raised every day writes an ok=False status with items 0; one that was not
    # even wired in leaves no row at all. Both are zero items over the week.
    _history(archive, day, 7, {"lesswrong": 4, "alignmentforum": 1})
    res = gate_source_minimums(archive, day, FETCHERS)
    assert any(d.startswith("eaforum: 0 items") for d in res.details), res.details


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
    # The two item-taking gates want different lists, and a mover is what separates them: it is
    # rendered (so provenance needs its URL) but it is an older post by definition (so freshness
    # must not be shown it, or the window check fails on the very thing that makes it a mover).
    post = make_item(id="lw:1", published_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    mover_item = make_item(
        id="lw:9",
        url="https://www.lesswrong.com/posts/9/y",
        published_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    movers = [Mover("lw:9", "Older post", mover_item.url, 12, 40, 60, None, rec("lw:9", 3))]
    rendered = render_all(META, build_render_items([post], [rec("lw:1", 3)]), movers, [], tokens)
    assert set(rendered) == {"31-today-forums.md", "00-index.md"}
    assert gate_provenance(rendered, [post, mover_item]).passed
    assert gate_freshness(rendered, [post], WINDOW_START, WINDOW_END, VERSION).passed
    assert not gate_freshness(
        rendered, [post, mover_item], WINDOW_START, WINDOW_END, VERSION
    ).passed
    # Only the forums file gets an expected count: the index's `- **` lines are recipes, not items.
    assert gate_budgets(rendered, S, tokens, {"31-today-forums.md": 2}).passed
