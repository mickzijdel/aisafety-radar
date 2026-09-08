"""Render the appendices from JSON. The templates write every metadata line and every link.

The model's output reaches this module as an `Extraction` and never as markdown: the title,
author, date, karma, comment count and URL on an item's metadata line all come from the `Item`
the fetcher built, so a model that hallucinates a link cannot get one published (spec 6.3). What
the model contributes is prose, and prose that failed verification is withheld rather than
softened -- a degraded item keeps its metadata line and its verified quotes and says plainly that
the summary could not be checked.

Two rendering decisions are worth stating because the obvious version of each is wrong:

* ForumMagnum's `extendedScore.agreement` is a *net* score, not the split that spec 4.3 shows,
  and 0 is what an unvoted post and a post whose agree and disagree votes cancelled both look
  like. Rendering `agree 0` would present the absence of a signal as a signal, so the segment is
  omitted unless the number is non-zero.
* A "discussion mover" can move on karma alone, so `+0 comments` is a real possibility (and a
  negative count is too, when comments are deleted between runs). The comment segment appears
  only when comments were actually added.

Whitespace is normalised on the way out (`_tidy`) rather than micro-managed with Jinja's
whitespace control. The templates then only have to get the *content* right and may be written
to read like the markdown they produce; the guarantee that the file has no trailing spaces, no
double blank lines and exactly one final newline lives in one place and is tested once.

**The structure invariant: all prose and display strings pass through `oneline`; quotes are the
only verbatim slot, and they render inside `> "..."`.** Everything a template interpolates apart
from a quote is third-party text (a post title, an author name, a model-written summary or
discussion signal), and markdown gives a string no way to say "I am content, not structure". A
summary arriving as `Contested.\\n\\n## Heading\\n\\n- **Fake post** ...` would otherwise close
the item block and render a real heading and a counterfeit item line: precisely the forgery the
template-owns-the-metadata-line rule (spec 6.3) exists to prevent, reintroduced one layer down.
`oneline` flattens the value to a single line, escapes a leading block marker and holds `<` to a
guillemet, so third-party text can only ever land *inside* the line the template put it on and
cannot act as HTML once it is there. Any interpolation added to a template needs `|oneline` unless
it is a number or one of this module's own constants. `tests/test_hostile_render.py` pushes one
forgery payload through every field that reaches the pack and holds the rendered shape to the
byte counts a clean pack produces.

The quote slot (`item.j2`) is the one exception, and "quotes are verbatim" is only half the
reason it is safe to pass a raw `<` there. The other half is that a quote with a raw `<` in it
cannot reach the pack in the first place: the model only ever sees a source through `data_block`,
which folds every `<` to a guillemet, so a quote it copies out of a bracket-bearing sentence is
written with the guillemet, fails `verify`'s `contains` check against the unfolded source, and is
dropped. What survives verification is therefore a substring of a document that had no `<` at
that point. The slot is still contained rather than trusted -- the quote is flattened onto one
line inside the `> "..."` the template owns -- but nothing has to escape it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pipeline.models import Item, SourceStatus
from pipeline.stages.comments import Mover
from pipeline.stages.verify import ExtractionRecord

# A line that markdown would read as opening a block rather than as text: an ATX heading, a
# bullet, or a blockquote. `#` and the bullet markers need the trailing space (`#foo` is not a
# heading); `>` does not (`>foo` is a blockquote). Anchored, so only a marker at the *start* of
# the flattened line can do structural damage; `a - b` mid-sentence is left alone.
_OPENS_A_BLOCK = re.compile(r"^(?:#{1,6}(?:\s|$)|[-*+](?:\s|$)|>)")

# `<` held to the single-guillemet `data_block` uses on the way into a prompt. Markdown passes raw
# HTML straight through, so this is the one character in third-party text that can still act after
# the text has been flattened onto one line. `>` is deliberately left alone: it opens a block only
# at the start of a line, which `_OPENS_A_BLOCK` already escapes, and neutralising it would mangle
# the `->` and `>` that ordinary titles and prose contain.
_ANGLE_OPEN = str.maketrans({"<": "‹"})


def oneline(text: str | None) -> str:
    """`text` flattened to one line, block markers escaped and angle brackets neutralised.

    Every prose and display value a template interpolates goes through this. Flattening is what
    stops third-party text escaping the line the template put it on; the backslash is what stops
    a *single*-line `## Heading` doing the same thing without needing a newline (an indented
    heading is still a heading: CommonMark allows up to three leading spaces, so the two-space
    indent under a metadata line is no protection).

    The angle brackets are the third way out, and the one flattening does not close. Markdown
    passes raw HTML through, so a post titled `<!-- ` hides every byte after it from anything that
    renders the pack, and a `<script>` in an author's display name is a script tag in whatever
    renders it. Every one of those needs a `<`, so `<` is held to `‹` -- the substitution
    `data_block` makes on the way into a prompt -- and the file still says what the source said,
    as text. Quotes are the one exception and do not come through here: they are the verbatim
    slot, verified as substrings of the source, and the template contains them inside `> "..."`
    instead.
    """
    if text is None:
        return ""
    line = " ".join(text.split()).translate(_ANGLE_OPEN)
    return "\\" + line if _OPENS_A_BLOCK.match(line) else line


def _plural(count: int, word: str) -> str:
    """`3 comments`, but `1 comment`."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _code_list(names: Iterable[str]) -> str:
    """File names as a comma-separated list of markdown code spans."""
    return ", ".join(f"`{name}`" for name in names)


_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)
_ENV.filters["oneline"] = oneline
_ENV.filters["plural"] = _plural
_ENV.filters["code_list"] = _code_list

SITE_LABELS = {"lw": "LW", "af": "AF", "eaf": "EAF"}

# Where the pack is published. One definition, because two consumers have to agree on it: the
# header links every appendix back to its latest version here, and the provenance gate treats
# this prefix as the one host a rendered link may point at without coming from a source. Two
# copies of this string would let a move break the gate open rather than break the build.
PAGES_BASE = "https://mickzijdel.github.io/aisafety-radar"

APPENDIX_PURPOSE: dict[str, str] = {
    "00-index.md": "this index and the recipes",
    "10-map-field-overview.md": "map: areas, live questions per area, who works on what",
    "11-map-orgs.md": "map: org directory",
    "12-map-programmes.md": "map: training, fellowships, courses",
    "13-map-glossary.md": "map: concepts, jargon, entity disambiguation, renamed entities",
    "14-map-readings.md": "map: canonical readings per area",
    "15-careers.md": "map: paths, 80k profiles, advisors, entry programmes, context",
    "16-map-field-context.md": "map: norms, culture, positions map, history, hiring realities",
    "20-current-state.md": "rolling: 30-day synthesis per area",
    "21-this-week.md": "rolling: 7-day view",
    "30-today.md": "daily: executive summary and top items across sources",
    "31-today-forums.md": "daily: LW / AF / EAF posts and discussion movers",
    "32-today-news.md": "daily: newsletters, journalism, lab blogs, governance, incidents",
    "33-today-papers.md": "daily: arXiv and lab papers",
    "40-events-training.md": "daily: upcoming events and the authoritative deadline list",
    "41-jobs-funding.md": "daily: new roles, funding rounds and deadlines",
    "50-forecasts-metrics.md": "daily: forecast movements, Epoch data, benchmarks",
    "60-imports-xrisk-daily.md": "verbatim: the latest X-Risk Daily briefing",
    "90-live-sources.md": "stable URLs and APIs for every source",
}

RECIPES: list[tuple[str, list[str]]] = [
    (
        "what happened today",
        ["30-today.md", "31-today-forums.md", "32-today-news.md", "33-today-papers.md"],
    ),
    ("this week", ["21-this-week.md"]),
    ("close look at an area", ["10-map-field-overview.md", "20-current-state.md"]),
    (
        "career change",
        [
            "15-careers.md",
            "16-map-field-context.md",
            "12-map-programmes.md",
            "40-events-training.md",
            "41-jobs-funding.md",
        ],
    ),
]


# Bundle file name per recipe name in `RECIPES`. The recipes are the source of truth for the file
# lists; this only names the download.
_RECIPE_BUNDLES = {
    "what happened today": "today.md",
    "this week": "week.md",
    "career change": "careers.md",
}

# The slow layer in one fetch. No recipe matches it: "a close look at one area" is two files
# chosen per area, not the whole map.
_MAP_BUNDLE = [
    "10-map-field-overview.md",
    "11-map-orgs.md",
    "12-map-programmes.md",
    "13-map-glossary.md",
    "14-map-readings.md",
]

_recipe_files = dict(RECIPES)
assert _RECIPE_BUNDLES.keys() <= _recipe_files.keys(), "a bundle names a recipe that is gone"

# What each downloadable bundle is made of. This lives here rather than in `pipeline.publish.site`,
# which is what writes the files, because the *index* has to name the same bundles the site
# actually publishes and the index is rendered a job earlier: a footer advertising `week.md`
# against a site that never wrote one sends readers to a 404. `site.py` imports it from here.
BUNDLES: dict[str, list[str]] = {
    bundle: _recipe_files[recipe] for recipe, bundle in _RECIPE_BUNDLES.items()
} | {"map.md": _MAP_BUNDLE}


@dataclass
class PackMeta:
    version: str
    generated_at: datetime
    data_through: datetime
    latest_base: str = f"{PAGES_BASE}/latest"


@dataclass
class RenderItem:
    title: str
    author: str
    affiliation: str | None
    site_label: str
    date: str
    score: int | None
    agreement_line: str | None
    comment_count: int
    url: str
    summary: str
    author_stated_status: str | None
    claim_type: str
    discussion_signal: str | None
    novelty: str | None
    quotes: list[str]
    degraded: bool
    importance: int


def _collapse(text: str | None) -> str | None:
    """`text` with every run of whitespace collapsed to one space, preserving None.

    `RenderItem` is the templates' input, and every field of it occupies exactly one line, so it
    is built with single-line values. This is *not* the structure guarantee: escaping belongs to
    render time, where `oneline` applies it to movers and statuses too, which never pass through
    here. Keeping the two separate leaves `RenderItem` holding readable data rather than
    backslash-escaped markdown.
    """
    return " ".join(text.split()) if text is not None else None


def _tidy(text: str) -> str:
    """Strip trailing spaces, collapse blank runs to one, end with exactly one newline."""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def build_render_items(items: list[Item], records: list[ExtractionRecord]) -> list[RenderItem]:
    """The items that have an extraction, as template input, most important first.

    An item with no extraction is dropped rather than rendered bare: triage decided it was not
    worth a model call, and a metadata line with no claim under it tells a reader nothing.
    """
    by_id = {r.extraction.item_id: r for r in records}
    out: list[RenderItem] = []
    for item in items:
        record = by_id.get(item.id)
        if record is None:
            continue
        e = record.extraction
        out.append(
            RenderItem(
                title=_collapse(item.title) or item.id,
                author=item.author,
                affiliation=_collapse(item.author_affiliation),
                site_label=SITE_LABELS.get(item.site or "", item.source),
                date=item.published_at.date().isoformat(),
                score=item.score,
                # Net agreement, and 0 is "nobody voted", not "the readers were neutral".
                agreement_line=f"agree {item.agreement}" if item.agreement else None,
                comment_count=item.comment_count,
                url=item.url,
                summary=_collapse(e.summary) or "",
                author_stated_status=_collapse(e.author_stated_status),
                claim_type=e.claim_type,
                discussion_signal=_collapse(e.discussion_signal),
                novelty=_collapse(e.novelty),
                quotes=[q for q in (_collapse(q) for q in e.key_quotes) if q],
                degraded=record.degraded,
                importance=e.importance,
            )
        )
    out.sort(key=lambda r: (r.importance, r.score or 0), reverse=True)
    return out


def render_forums(
    meta: PackMeta,
    items: list[RenderItem],
    movers: list[Mover],
    statuses: list[SourceStatus],
) -> str:
    return _tidy(
        _ENV.get_template("31-today-forums.md.j2").render(
            meta=meta,
            layer="daily",
            filename="31-today-forums.md",
            items=items,
            movers=movers,
            statuses=statuses,
        )
    )


def render_index(
    meta: PackMeta,
    appendices: dict[str, str],
    counter: Callable[[str], int],
    static: dict[str, str] | None = None,
) -> str:
    """The index: one row per appendix in spec 4.1, then the recipes with live token totals.

    Every appendix is listed whether or not this pack has it, so a reader can tell the difference
    between "this pack has no jobs appendix yet" and "there were no jobs today". What the index
    must never do is get that distinction backwards, and there are two ways it can:

    * `static` is the appendices the pack ships without this run rendering them -- today that is
      the hand-maintained `90-live-sources.md`, which sits in the skill's references directory
      next to the index itself. Marking a file the reader already has as "not yet published" sends
      them looking for something in their own folder. They are dated `static` rather than with this
      run's date, because nothing regenerated them today.
    * The bundle footer names only the bundles the site will actually write, computed from the
      files this pack has by the same rule `site._write_bundles` applies (a bundle exists when at
      least one of its parts does). A footer advertising `week.md` against a site that never wrote
      one hands the reader a 404, and the recipes above it already say what is missing.
    """
    counts = {name: counter(text) for name, text in appendices.items()}
    shipped = {name: counter(text) for name, text in (static or {}).items()}
    rows = [
        {
            "name": name,
            "purpose": purpose,
            "tokens": counts.get(name, shipped.get(name, "-")),
            "generated": (
                meta.generated_at.date().isoformat()
                if name in counts
                else ("static" if name in shipped else "not yet published")
            ),
        }
        for name, purpose in APPENDIX_PURPOSE.items()
        if name != "00-index.md"
    ]
    recipes = [
        {
            "name": name,
            "files": [f for f in files if f in counts],
            "missing": [f for f in files if f not in counts],
            "tokens": sum(counts[f] for f in files if f in counts),
        }
        for name, files in RECIPES
    ]
    have = set(counts) | set(shipped)
    bundles = [b for b, parts in BUNDLES.items() if any(p in have for p in parts)]
    return _tidy(
        _ENV.get_template("00-index.md.j2").render(
            meta=meta,
            layer="index",
            filename="00-index.md",
            rows=rows,
            recipes=recipes,
            bundles=bundles,
        )
    )


def _static_references(directory: Path | None, generated: set[str]) -> dict[str, str]:
    """The appendices already sitting in `directory` that this run does not render itself.

    The pack is written into the skill's references directory, which also holds the files nobody
    generates -- `90-live-sources.md` today. Reading them here is what lets the index tell "shipped
    with the skill" from "not written yet".

    Two exclusions, both load-bearing. Files this run *does* render are skipped even when an older
    copy is on disk, or a rerun would count yesterday's bytes for today's file. Names that are not
    appendices are skipped because the index has one row per name in `APPENDIX_PURPOSE` and
    nothing else has a row to appear in.
    """
    if directory is None or not directory.is_dir():
        return {}
    return {
        path.name: path.read_text()
        for path in sorted(directory.glob("*.md"))
        if path.name in APPENDIX_PURPOSE and path.name not in generated
    }


def render_all(
    meta: PackMeta,
    forums_items: list[RenderItem],
    movers: list[Mover],
    statuses: list[SourceStatus],
    counter: Callable[[str], int],
    static_dir: Path | None = None,
) -> dict[str, str]:
    """Every file this phase renders, keyed by the name it is published under.

    `static_dir` is where the pack will be written, if the caller knows: the index reads it for
    the appendices that ship without being generated, so it can report them as published rather
    than as missing. Only the rendered files come back in the return value; the static ones are
    already on disk and rewriting them is not this module's job.
    """
    forums = render_forums(meta, forums_items, movers, statuses)
    generated = {"31-today-forums.md", "00-index.md"}
    static = _static_references(static_dir, generated)
    index = render_index(meta, {"31-today-forums.md": forums}, counter, static)
    return {"31-today-forums.md": forums, "00-index.md": index}
