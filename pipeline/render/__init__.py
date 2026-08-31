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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pipeline.models import Item, SourceStatus
from pipeline.stages.comments import Mover
from pipeline.stages.verify import ExtractionRecord


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
_ENV.filters["plural"] = _plural
_ENV.filters["code_list"] = _code_list

SITE_LABELS = {"lw": "LW", "af": "AF", "eaf": "EAF"}

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


@dataclass
class PackMeta:
    version: str
    generated_at: datetime
    data_through: datetime
    latest_base: str = "https://mickzijdel.github.io/aisafety-radar/latest"


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


def _one_line(text: str | None) -> str | None:
    """`text` with every run of whitespace collapsed to one space, or None.

    Every field the item template renders occupies exactly one line, indented under the metadata
    line. A summary or quote that arrived with a newline in it would break out of that block and
    read as a new top-level paragraph, so the newline is spent here rather than in the template.
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
                title=_one_line(item.title) or item.id,
                author=item.author,
                affiliation=_one_line(item.author_affiliation),
                site_label=SITE_LABELS.get(item.site or "", item.source),
                date=item.published_at.date().isoformat(),
                score=item.score,
                # Net agreement, and 0 is "nobody voted", not "the readers were neutral".
                agreement_line=f"agree {item.agreement}" if item.agreement else None,
                comment_count=item.comment_count,
                url=item.url,
                summary=_one_line(e.summary) or "",
                author_stated_status=_one_line(e.author_stated_status),
                claim_type=e.claim_type,
                discussion_signal=_one_line(e.discussion_signal),
                novelty=_one_line(e.novelty),
                quotes=[q for q in (_one_line(q) for q in e.key_quotes) if q],
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


def render_index(meta: PackMeta, appendices: dict[str, str], counter: Callable[[str], int]) -> str:
    """The index: one row per appendix in spec 4.1, then the recipes with live token totals.

    Every appendix is listed whether or not this run produced it, so a reader can tell the
    difference between "this pack has no jobs appendix yet" and "there were no jobs today".
    """
    counts = {name: counter(text) for name, text in appendices.items()}
    rows = [
        {
            "name": name,
            "purpose": purpose,
            "tokens": counts[name] if name in counts else "-",
            "generated": (
                meta.generated_at.date().isoformat() if name in counts else "not yet published"
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
    return _tidy(
        _ENV.get_template("00-index.md.j2").render(
            meta=meta, layer="index", filename="00-index.md", rows=rows, recipes=recipes
        )
    )


def render_all(
    meta: PackMeta,
    forums_items: list[RenderItem],
    movers: list[Mover],
    statuses: list[SourceStatus],
    counter: Callable[[str], int],
) -> dict[str, str]:
    """Every file this phase renders, keyed by the name it is published under."""
    forums = render_forums(meta, forums_items, movers, statuses)
    index = render_index(meta, {"31-today-forums.md": forums}, counter)
    return {"31-today-forums.md": forums, "00-index.md": index}
