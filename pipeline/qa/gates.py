"""The QA gates a run has to clear before its pack is published (spec 6.8).

Each gate is a function over material the run already produced, and each returns a
`GateResult` rather than raising: the point of a gate is to say precisely what is wrong, and a
traceback names one problem where a run may have several. All but one are pure; the exception is
`gate_source_minimums`, which reads the last week of `run.json` reports out of the archive,
because the thing it checks -- a fetcher that has quietly stopped matching anything -- is
invisible inside a single day's counts. `blocking_failures` is what the caller
turns that list into a decision with. Spec 6.8's five gates block publication; the cost gate and
the 7-day source minimums alert without blocking, because an expensive run and a quiet week are
both things to look at, not reasons to withhold a correct digest.

Two of the gates deserve their reasoning stated, because the obvious reading of each is too weak:

* **Provenance.** The renderer owns every metadata line and every link on it, so a hallucinated
  link cannot reach a metadata line. What the model does contribute is prose, and prose is
  flattened and escaped on the way out but *not* rewritten -- a `https://` inside a summary passes
  through verbatim. This gate is therefore the thing standing between a model-invented URL and a
  published one. A URL clears it only by being an item's own URL, a URL that appears in the text
  the fetcher brought back with an item (its markdown, title, author or affiliation), or a link
  back into the pack's own site.

  This gate's two failure modes are not symmetrical: a missed link publishes one bad URL, while a
  false positive withholds the entire day's pack and leaves the run no way to recover. Every
  widening in the matching below is a false positive that actually happened.

  The corollary is a requirement on the caller: `items` must cover everything the files render,
  discussion movers included. Movers are rendered from ledger entries rather than `Item`s, so the
  caller has to supply an `Item` for each rendered mover or the mover's own link reads as
  unsourced. Note that this is *not* the same list `gate_freshness` wants: a mover is an older
  post by definition, so passing it to the freshness gate fails the window check on the very
  thing that made it a mover. Provenance takes posts plus movers; freshness takes today's posts.
  A mover also renders the display name of its new top comment's author, and that string belongs
  to no item at all, so the caller hands it in through `source_text`.

* **Budgets.** The item count is taken by counting rendered `- **` lines, which is what the item
  and mover templates both emit. `expected_items` for the forums file is therefore posts *plus*
  movers; passing the post count alone fails the gate. Files with no entry in `expected_items`
  are not counted at all, which is what the index wants -- its `- **` lines are recipes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import get_args

from pipeline.archive import Archive
from pipeline.config import Settings
from pipeline.fetchers.base import Fetcher
from pipeline.models import GateResult, Item, Site, Usage
from pipeline.render import PAGES_BASE
from pipeline.stages.scan import JudgeResult, ScanResult
from pipeline.stages.verify import ExtractionRecord

OWN_BASE = f"{PAGES_BASE}/"

# Named rather than spelled twice: `pipeline.publish.health` renders this gate's details as their
# own section of the pinned issue, and has to find the gate in the report by name.
SOURCE_MINIMUMS_GATE = "source-minimums"

# What ends a link in running text: whitespace, the closing half of a markdown or HTML wrapper,
# a quote, or a code-span backtick. Everything up to one of those is part of the link.
_END = r"[^\s)\]>\"'`]"

# Three shapes of the same thing. A link does not need a scheme to be followed -- an agent told to
# "check the source" will resolve `evil.example/x` as readily as `https://evil.example/x`, and a
# reader's client will linkify `www.evil.example/x` -- so matching only `https://` would leave the
# gate trivially bypassable by a model that omits the scheme. The scheme is matched
# case-insensitively for the same reason: `HTTPS://` is a working link.
#
# The bare-host pattern requires a TLD-shaped final label (`[a-z]{2,}`) before the slash. Without
# it, `GPT-4.5/turbo`, `4.5/5` and `v1.2/beta` all read as links, and a false positive here blocks
# the entire pack from publishing -- a far worse failure than the one being prevented. With it,
# both goldens and the rendered index produce zero false positives (asserted by test).
#
# What that leaves through, stated rather than implied: a *scheme-less* host that is not
# TLD-shaped still passes -- `evil.example:8080/x` (a port between the host and the slash),
# `192.0.2.1/x` (an all-digit final label), `xn--80ak6aa92e/x` (punycode's digits), and a
# homoglyph host whose non-ASCII characters this ASCII pattern cannot see. Each of those is caught
# the moment it carries `https://` or `www.`, and the scan gate reads the same prose for
# instructions, so this is a known residual rather than the only line of defence. Widening the
# bare-host pattern to cover them is what produced the false positives above.
_LINK_RES = (
    re.compile(rf"https?://{_END}+", re.IGNORECASE),
    re.compile(rf"\bwww\.{_END}+", re.IGNORECASE),
    re.compile(rf"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{{2,}}/{_END}+", re.IGNORECASE),
)

# Anything of the shape `[P...]`, so a malformed citation is reported rather than quietly skipped
# by a pattern that only recognises well-formed ones.
_PID_RE = re.compile(r"\[P([^\]\s]+)\]")

# The site prefixes an item id can carry, taken from the type rather than repeated here: a fourth
# forum added to `Site` has to start resolving citations on the day it starts producing items.
CITATION_SITES: frozenset[str] = frozenset(get_args(Site))
_ITEM_LINE_RE = re.compile(r"^- \*\*", re.MULTILINE)

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
# Sentence punctuation a URL collects at the end of a clause, the trailing slash (not a distinction
# any source makes), and the markdown emphasis markers. A link at the end of a bolded run comes out
# of `_links` as `host.example/x**`, which can never equal anything in the allowed set: the item
# title on every rendered metadata line is wrapped in `**`, so without this a post whose title is
# a URL blocks the pack. Stripped rather than excluded from `_END`, so that a URL with an
# underscore *inside* it stays whole and keeps being compared in full.
_TRAILING = "/.,*_"


def _trim(url: str) -> str:
    """A link with its scheme and trailing punctuation removed, but its case left alone.

    This is the form a failure is *reported* in: the reader of a blocked run has to go and look
    the link up, and a path folded to lower case may not be the path the source carries.
    """
    return _SCHEME_RE.sub("", url).rstrip(_TRAILING)


def _normalise(url: str) -> str:
    """A link reduced to the form the allowed set is keyed on: no scheme, no trailing punctuation.

    Dropping the scheme is what lets a legitimately scheme-less rendering of an item's own URL
    match the item it came from. It also means the gate does not distinguish `http` from `https`;
    that is deliberate -- provenance asks where a link points, not how it is fetched.

    Case is folded for the same reason, on both sides: the allowed set is built through this
    function too. Hosts are case-insensitive outright, and while paths are not, a model that
    re-cased a path it copied out of a source was blocking the whole pack over a link that goes
    exactly where the source's link goes.
    """
    return _trim(url).lower()


def _link_map(text: str) -> dict[str, str]:
    """Every link-like token in `text`: normalised form -> the way it was first written.

    The three patterns overlap by design -- a fully written URL matches the schemed pattern and
    the bare-host one -- so matches are de-duplicated on the normalised form, or one bad link would
    be reported two or three times. The written form is kept so a failure can name the link as the
    file spells it rather than as the comparison spells it.
    """
    found = sorted(
        (m.start(), _normalise(m.group()), _trim(m.group()))
        for r in _LINK_RES
        for m in r.finditer(text)
    )
    out: dict[str, str] = {}
    for _, link, written in found:
        out.setdefault(link, written)
    return out


def _links(text: str) -> list[str]:
    """Every link-like token in `text`, normalised, de-duplicated, in order of appearance."""
    return list(_link_map(text))


_OWN_BASE_NORM = _normalise(OWN_BASE)


def _is_own(url: str) -> bool:
    """True for the pack's own site: the base itself, or anything under it.

    Matching on the base plus a separator rather than on the bare prefix, so a lookalike host path
    (`.../aisafety-radar-mirror/x`) does not inherit the allowance.
    """
    return url == _OWN_BASE_NORM or url.startswith(_OWN_BASE_NORM + "/")


def _is_citation(body: str) -> bool:
    """True when `[P<body>]` is trying to be a citation rather than a bracketed word.

    Item ids are `<site>:<local-id>` for one of the sites this pipeline fetches, so that prefix is
    the mark of intent; an all-digit body is the other way a citation comes out malformed. Without
    this, `[PDF]`, `[Paper]` and the `[Post]` of a markdown link would each fail the gate and block
    the pack -- prose, not broken links.

    A colon alone is not enough, which is the narrowing this function has been through: `[Python:
    3.12]`, `[Pytorch:2.4]` and `[Priority:high]` are all prose with a colon in them, and each one
    withheld an otherwise correct pack. The cost of the narrowing is that `[Pzz:abc123]`, naming a
    site the pipeline does not fetch, now reads as prose too -- it renders no link and resolves to
    nothing for anyone, where a false positive costs the day's publication. The prefix is compared
    case-insensitively so a `[PLW:abc]` is still held to the ids the pack actually carries.

    A site prefix alone is not enough either, and for the same reason from the other direction:
    `[Plw]`, `[Paf]` and `[Peaf]` are the three site names with the citation's own `P` in front,
    and a body with no colon carries no local id to resolve. Both halves of the shape are
    therefore required -- the colon and a known site before it -- so a bracketed word can never
    withhold a pack, while `[Plw:]` (a site prefix with the colon and nothing after it) is still a
    citation attempt and still has to resolve.
    """
    prefix, colon, _ = body.partition(":")
    return (bool(colon) and prefix.lower() in CITATION_SITES) or body.isdigit()


def gate_provenance(
    rendered: dict[str, str], items: list[Item], source_text: Sequence[str] = ()
) -> GateResult:
    """Every link and `[P<id>]` citation in the rendered files traces back to a source.

    `source_text` is fetched third-party text the renderer copies into the pack that belongs to no
    `Item`: a discussion mover's new top comment renders its commenter's display name, and that
    name arrives down the mover path, which carries a ledger entry and an archived record rather
    than an item. It is treated exactly as an item's title or author is -- the links *inside* each
    string are allowed, and nothing else -- so a forum user whose display name is a URL cannot
    withhold the pack, and cannot vouch for any other link either.
    """
    allowed = {_normalise(i.url) for i in items}
    for text in source_text:
        allowed.update(_links(text))
    for i in items:
        # Spec 6.8 gate 1 allows a link that "appears verbatim in fetched source text", and the
        # title, author and affiliation are fetched source text as much as the body is -- the
        # renderer copies them onto the metadata line untouched. A post titled after a URL, or a
        # forum user whose display name is one, would otherwise read as a model invention and
        # block the pack over a string the pipeline itself put there.
        allowed.update(_links(i.markdown))
        allowed.update(_links(i.title))
        allowed.update(_links(i.author))
        allowed.update(_links(i.author_affiliation or ""))
    ids = {i.id for i in items}
    details: list[str] = []
    for name, text in rendered.items():
        for url, written in _link_map(text).items():
            if url not in allowed and not _is_own(url):
                details.append(f"{name}: URL not in any source: {written}")
        for pid in _PID_RE.findall(text):
            if _is_citation(pid) and pid not in ids:
                details.append(f"{name}: unresolved item reference [P{pid}]")
    return GateResult(name="provenance", passed=not details, blocking=True, details=details)


def gate_budgets(
    rendered: dict[str, str],
    settings: Settings,
    counter: Callable[[str], int],
    expected_items: dict[str, int],
) -> GateResult:
    """Each file inside its token budget, and rendering exactly the items it was told to expect.

    `expected_items` is not the selected count. `run_daily` passes the *rendered* item count plus
    the movers, so this is a rendered-versus-rendered check: it catches a template that dropped or
    duplicated an entry, not a pack that shrank between selection and rendering. The gap between
    selected and rendered is deliberately somebody else's job -- `RunReport.dropped` names every
    item that fell out, and `health.refusal_reason` is what refuses to promote a pack the drops
    emptied. A gate that failed on a shrunken-but-honest pack would withhold a correct day.
    """
    details: list[str] = []
    for name, text in rendered.items():
        stem = name.removesuffix(".md")
        budget = settings.budgets.get(stem)
        tokens = counter(text)
        if budget is not None and tokens > budget:
            details.append(f"{name}: {tokens} tokens over budget {budget}")
        if name in expected_items:
            found = len(_ITEM_LINE_RE.findall(text))
            if found != expected_items[name]:
                details.append(f"{name}: {found} items rendered, expected {expected_items[name]}")
    return GateResult(name="budgets", passed=not details, blocking=True, details=details)


def gate_freshness(
    rendered: dict[str, str],
    items: list[Item],
    window_start: datetime,
    window_end: datetime,
    version: str,
) -> GateResult:
    """Every file stamped with this run's version, and every item published inside the window.

    The window is half-open: an item timestamped exactly at `window_end` belongs to the next run,
    and counting it in both would publish it twice.
    """
    details: list[str] = []
    # The separator is part of the match: a version is a prefix of the next suffixed one, so
    # stopping at the version alone would let a `2026.08.30ab` pack pass as `2026.08.30a`.
    prefix = f"<!-- aisafety-radar · version {version} ·"
    for name, text in rendered.items():
        if not text.startswith(prefix):
            details.append(f"{name}: missing or wrong header")
    for i in items:
        if not (window_start <= i.published_at < window_end):
            details.append(f"{i.id}: published {i.published_at.isoformat()} outside window")
    return GateResult(name="freshness", passed=not details, blocking=True, details=details)


def gate_scan(scans: dict[str, ScanResult], rendered_ids: set[str]) -> GateResult:
    """No item the injection scan flagged reached the rendered pack."""
    details = [
        f"{iid}: flagged by injection scan ({s.reason})"
        for iid, s in scans.items()
        if s.flagged and iid in rendered_ids
    ]
    return GateResult(name="injection-scan", passed=not details, blocking=True, details=details)


def gate_judge(judges: dict[str, JudgeResult], records: dict[str, ExtractionRecord]) -> GateResult:
    """Every important item that failed the faithfulness judge was degraded rather than published.

    Only importance 4 and 5 block: those are judged on every run, so a failure there is a measured
    fact. The rest are judged on a sample, and blocking the pack on one sampled failure would make
    the sample rate a lottery over whether the digest ships.
    """
    details: list[str] = []
    for iid, j in judges.items():
        rec = records.get(iid)
        if rec is None or j.passed or rec.degraded:
            continue
        if rec.extraction.importance >= 4:
            details.append(f"{iid}: importance {rec.extraction.importance} failed judge: {j.notes}")
    return GateResult(name="faithfulness-judge", passed=not details, blocking=True, details=details)


def gate_source_minimums(archive: Archive, day: date, fetchers: Sequence[Fetcher]) -> GateResult:
    """Every source and every category above its rolling 7-day minimum item count (spec 6.1).

    This is the live check on the fetchers: spec 11 says so explicitly, because a fetcher that has
    quietly stopped matching anything looks exactly like a quiet week from inside one run. Only a
    week of them tells the two apart, so the counts come out of the last seven recorded `run.json`
    reports rather than out of today.

    Three decisions in it:

    * **Raw counts, the same ones the medians read.** `statuses[].items` is what the fetcher
      returned before the floor, which is the number a broken selector or a moved API actually
      moves. Post-floor survivors would fold a karma-threshold change into the same number.
    * **A category's minimum is the sum of its sources'.** Spec 6.1 gives every category its own
      minimum but no configuration carries one, and the sum of the member fetchers' floors is the
      only figure the code actually knows. Being a sum of the same counts, the category row cannot
      fire while every one of its sources clears its own floor -- it is a rollup, not a second
      independent check, and it is here because "the forums layer produced 7 of an expected 23
      this week" is the sentence spec 6.1 asks for and three per-source rows do not say.
    * **Never blocking.** A week under the floor is a thing for a human to look at, not a reason
      to withhold a correct digest; spec 6.9 makes three consecutive runs the health failure, and
      that judgement belongs to whoever reads the pinned issue.

    With fewer than seven reports to read there is no week to judge, so the gate passes and says
    why. A pipeline started on Wednesday would otherwise alert every morning until Sunday.
    """
    reports = archive.recent_runs(day, 7)
    if len(reports) < 7:
        return GateResult(
            name=SOURCE_MINIMUMS_GATE,
            passed=True,
            blocking=False,
            details=[
                f"{len(reports)} of the last 7 runs recorded: too little history to judge a week"
            ],
        )
    totals: dict[str, int] = {}
    for report in reports:
        for status in report.get("statuses", []):
            totals[status["source"]] = totals.get(status["source"], 0) + int(status["items"])
    details: list[str] = []
    for f in fetchers:
        # A fetcher with no row at all in the reports counts as zero, not as unknown: a source
        # that was never reached wrote nothing, and that is the failure the gate is looking for.
        got = totals.get(f.name, 0)
        if got < f.min_items_7d:
            details.append(f"{f.name}: {got} items in 7 days, below the minimum {f.min_items_7d}")
    by_category: dict[str, list[Fetcher]] = {}
    for f in fetchers:
        by_category.setdefault(f.category, []).append(f)
    for category, members in by_category.items():
        got = sum(totals.get(f.name, 0) for f in members)
        minimum = sum(f.min_items_7d for f in members)
        if got < minimum:
            details.append(
                f"category {category}: {got} items in 7 days, below the minimum {minimum}"
            )
    return GateResult(
        name=SOURCE_MINIMUMS_GATE, passed=not details, blocking=False, details=details
    )


def gate_cost(usage: Usage, trailing_median: float | None, multiplier: float) -> GateResult:
    """Alert when the run cost more than `multiplier` times the trailing median. Never blocks.

    With no median yet -- the first runs, or a reset history -- there is nothing to compare
    against, and inventing a threshold would alert on noise.
    """
    if trailing_median is None or usage.cost_usd <= multiplier * trailing_median:
        return GateResult(name="cost", passed=True, blocking=False)
    return GateResult(
        name="cost",
        passed=False,
        blocking=False,
        details=[
            f"run cost ${usage.cost_usd:.2f} exceeds "
            f"{multiplier}x trailing median ${trailing_median:.2f}"
        ],
    )


def blocking_failures(results: list[GateResult]) -> list[GateResult]:
    """The gates that failed and block publication, in the order they were run."""
    return [r for r in results if r.blocking and not r.passed]
