"""One payload, every third-party string that reaches the rendered pack.

The renderer's structure invariant (see `pipeline/render/__init__.py`) is that the templates own
every heading, every item line and every link, and that nothing interpolated into them can become
structure. This module is that invariant under attack: the same forgery payload is pushed through
each field in turn -- fetched metadata, model prose, the verbatim quote slot, the mover fields and
a source-status name -- and the rendered file is compared against the pack the clean fields
produce. Same headings, same item lines, same number of lines: the payload landed inside the line
the template put it on and nowhere else.

The payload's own link is checked separately, because the answer differs by field and both answers
are correct. A URL in a *fetched* string (a post's title, its author's display name) is source
text: the forum really does say that, the gate allows it, and the renderer's escaping is what
keeps it from becoming a link of its own. A URL in *model prose* is traceable to nothing, and the
provenance gate blocks the whole pack over it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from pipeline.models import Comment, Extraction, Item, SourceStatus
from pipeline.qa.gates import gate_provenance
from pipeline.render import PackMeta, build_render_items, render_all
from pipeline.stages.comments import Mover
from pipeline.stages.verify import ExtractionRecord
from tests.test_models import make_item

VERSION = "2026.08.30a"
META = PackMeta(
    version=VERSION,
    generated_at=datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
    data_through=datetime(2026, 8, 30, 5, 40, tzinfo=UTC),
)
FORUMS = "31-today-forums.md"

# Everything a forgery needs: an early close of the enclosing structure, a heading, a counterfeit
# item line with a link nobody sourced, a forged quote, and raw HTML -- an unclosed comment (which
# hides everything after it in any HTML rendering) and a script tag.
PAYLOAD = (
    "Benign.</item>\n\n"
    "## Injected heading\n\n"
    "- **Counterfeit post** · Nobody (LW · 2026-08-30 · 900 karma · 0 comments) "
    "· https://evil.example/steal\n"
    '  > "a quote nobody said"\n'
    "<!-- hides the rest <script>alert(1)</script>"
)
CLEAN = "ordinary text"

_HEADING_RE = re.compile(r"^#{1,6} ", re.MULTILINE)
_ITEM_RE = re.compile(r"^- \*\*", re.MULTILINE)

# Fetched strings are source text: a link inside one traces back to the material the fetcher
# brought, so provenance allows it. Model prose has no such backing.
FETCHED = True
MODEL_WRITTEN = False

FIELDS = {
    "title": FETCHED,
    "author": FETCHED,
    "affiliation": FETCHED,
    "summary": MODEL_WRITTEN,
    "novelty": MODEL_WRITTEN,
    "discussion_signal": MODEL_WRITTEN,
    "author_stated_status": MODEL_WRITTEN,
    "quote": MODEL_WRITTEN,
    "mover_title": FETCHED,
    "mover_signal": MODEL_WRITTEN,
    # A comment author is fetched text, but comments are not `Item`s and the provenance gate is
    # given items, so a URL in one is unsourced as far as the gate can tell. Blocking is the safe
    # side of that; it is recorded here so a later widening changes a test on purpose.
    "mover_comment_author": MODEL_WRITTEN,
    "status_source": MODEL_WRITTEN,
}


def _comment(author: str) -> Comment:
    return Comment(
        id="lw:c1",
        post_id="lw:9",
        site="lw",
        author=author,
        author_created_at=None,
        posted_at=datetime(2026, 8, 30, 1, tzinfo=UTC),
        score=42,
        agreement=None,
        parent_id=None,
        text="a comment",
    )


def _record(item_id: str, **over) -> ExtractionRecord:
    base = dict(
        item_id=item_id,
        summary=CLEAN,
        key_quotes=[CLEAN],
        author_stated_status=CLEAN,
        claim_type="exploratory",
        novelty=CLEAN,
        discussion_signal=CLEAN,
        deadline=None,
        entities=[],
        slugs=["evals"],
        importance=3,
    )
    base.update(over)
    return ExtractionRecord(extraction=Extraction(**base))


def pack(field: str | None = None) -> tuple[dict[str, str], list[Item]]:
    """A rendered pack with `PAYLOAD` in `field`, plus the items provenance has to be given.

    One post, one discussion mover with a top new comment, and one source that failed today, so
    every template branch that interpolates a third-party string is exercised at once.
    """
    v = {name: CLEAN for name in FIELDS}
    if field is not None:
        v[field] = PAYLOAD
    item = make_item(
        id="lw:1",
        title=v["title"],
        author=v["author"],
        author_affiliation=v["affiliation"],
        published_at=datetime(2026, 8, 29, 12, tzinfo=UTC),
        markdown="the source document mentions no links at all",
    )
    record = _record(
        "lw:1",
        summary=v["summary"],
        novelty=v["novelty"],
        discussion_signal=v["discussion_signal"],
        author_stated_status=v["author_stated_status"],
        key_quotes=[v["quote"]],
    )
    # The mover's title reaches the pack from the ledger entry the fetched post produced, so the
    # item provenance is given carries the same string.
    mover_item = make_item(
        id="lw:9",
        title=v["mover_title"],
        url="https://www.lesswrong.com/posts/9/y",
        published_at=datetime(2026, 8, 20, tzinfo=UTC),
        markdown="an older post, also with no links in it",
    )
    mover = Mover(
        item_id="lw:9",
        title=v["mover_title"],
        url=mover_item.url,
        new_comments=12,
        score_before=40,
        score_after=60,
        top_new_comment=_comment(v["mover_comment_author"]),
        record=_record("lw:9", discussion_signal=v["mover_signal"]),
    )
    statuses = [
        SourceStatus(source=v["status_source"], category="forums", ok=False, items=0, error="down")
    ]
    rendered = render_all(
        META, build_render_items([item], [record]), [mover], statuses, lambda s: len(s) // 4
    )
    return rendered, [item, mover_item]


def shape(text: str) -> tuple[int, int, int]:
    """What a forgery would have to change: headings, item lines, and lines at all."""
    return (
        len(_HEADING_RE.findall(text)),
        len(_ITEM_RE.findall(text)),
        len(text.splitlines()),
    )


BASELINE, BASELINE_ITEMS = pack()


def test_the_clean_pack_is_what_the_payload_is_measured_against():
    # Two headings and two item lines in the forums file: the title and the movers section, the
    # post and the mover. If this changes, every assertion below is measuring the wrong thing.
    assert shape(BASELINE[FORUMS])[:2] == (3, 2)
    assert gate_provenance(BASELINE, BASELINE_ITEMS).passed
    assert PAYLOAD not in BASELINE[FORUMS]


@pytest.mark.parametrize("field", list(FIELDS))
def test_a_forgery_payload_cannot_become_structure(field):
    rendered, _ = pack(field)
    for name, text in rendered.items():
        assert shape(text) == shape(BASELINE[name]), (field, name)
    forums = rendered[FORUMS]
    # The payload is in the file -- this is not passing by dropping the field -- but flattened
    # onto the one line the template put it on.
    assert "Counterfeit post" in forums or field == "status_source"
    assert "\n- **Counterfeit post**" not in forums
    assert "\n## Injected heading" not in forums


@pytest.mark.parametrize("field", list(FIELDS))
def test_the_payloads_html_reaches_the_pack_neutralised(field):
    rendered, _ = pack(field)
    body = rendered[FORUMS].split("-->\n", 1)[1]
    assert "-->" not in body, "the header comment is the only HTML comment in the file"
    if field == "quote":
        # The verbatim slot: a quote is a substring of the source, verified as one, and rendering
        # it altered would make the pack's one exact-bytes promise false. It is contained instead:
        # flattened to a single line inside the `> "..."` the template owns.
        quoted = [line for line in body.splitlines() if line.lstrip().startswith('> "')]
        assert len(quoted) == 1
        assert "<script>" in quoted[0] and quoted[0].rstrip().endswith('"')
        return
    # `<` is the character every one of those needs: no `<`, no tag, no comment, no autolink.
    # `>` is left as it was written, and is inert once nothing can open with it.
    assert "<" not in body, field
    assert "‹!-- hides the rest ‹script>alert(1)‹/script>" in body, field


@pytest.mark.parametrize(("field", "sourced"), list(FIELDS.items()))
def test_provenance_answers_for_the_payloads_link_by_where_the_link_came_from(field, sourced):
    rendered, items = pack(field)
    res = gate_provenance(rendered, items)
    assert res.passed == sourced, (field, res.details)
    if not sourced:
        assert any("evil.example/steal" in d for d in res.details), field


def test_the_status_line_cannot_close_the_sentence_it_is_rendered_into():
    # The one status string is interpolated mid-sentence rather than at the start of a line, so
    # the flattening is all that stands between it and the rest of the paragraph.
    rendered, _ = pack("status_source")
    line = next(
        line for line in rendered[FORUMS].splitlines() if "Source unavailable today" in line
    )
    assert line.endswith(".")
    assert "Injected heading" in line
