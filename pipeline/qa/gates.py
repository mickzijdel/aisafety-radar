"""The six QA gates a run has to clear before its pack is published (spec 6.8).

Each gate is a pure function over material the run already produced, and each returns a
`GateResult` rather than raising: the point of a gate is to say precisely what is wrong, and a
traceback names one problem where a run may have several. `blocking_failures` is what the caller
turns that list into a decision with -- five of the six gates block publication, and the cost gate
alerts without blocking, because an expensive run is a thing to look at, not a reason to withhold
a correct digest.

Two of the gates deserve their reasoning stated, because the obvious reading of each is too weak:

* **Provenance.** The renderer owns every metadata line and every link on it, so a hallucinated
  link cannot reach a metadata line. What the model does contribute is prose, and prose is
  flattened and escaped on the way out but *not* rewritten -- a `https://` inside a summary passes
  through verbatim. This gate is therefore the thing standing between a model-invented URL and a
  published one. A URL clears it only by being an item's own URL, a URL that appears in an item's
  source markdown, or a link back into the pack's own site.

  The corollary is a requirement on the caller: `items` must cover everything the files render,
  discussion movers included. Movers are rendered from ledger entries rather than `Item`s, so the
  caller has to supply an `Item` for each rendered mover or the mover's own link reads as
  unsourced.

* **Budgets.** The item count is taken by counting rendered `- **` lines, which is what the item
  and mover templates both emit. `expected_items` for the forums file is therefore posts *plus*
  movers; passing the post count alone fails the gate. Files with no entry in `expected_items`
  are not counted at all, which is what the index wants -- its `- **` lines are recipes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from pipeline.config import Settings
from pipeline.models import GateResult, Item, Usage
from pipeline.stages.scan import JudgeResult, ScanResult
from pipeline.stages.verify import ExtractionRecord

# Deliberately greedy up to the first delimiter markdown or prose would use to close a link, so a
# URL inside `[x](...)`, `<...>` or a quoted string is captured without its wrapper.
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_PID_RE = re.compile(r"\[P([a-z]+:[A-Za-z0-9_-]+)\]")
_ITEM_LINE_RE = re.compile(r"^- \*\*", re.MULTILINE)
OWN_BASE = "https://mickzijdel.github.io/aisafety-radar/"

# Punctuation a URL collects from the text around it: sentence punctuation, the closing backtick
# of a code span (the index renders links inside one), and the trailing slash, which is not a
# distinction any source makes. Both sides of the comparison go through `_normalise`, so a source
# URL written with a trailing dot still matches the same URL rendered without one. Stripping only
# ever shortens a candidate, so it cannot admit a URL that would otherwise have been flagged.
_TRAILING = "/.,`"


def _normalise(url: str) -> str:
    return url.rstrip(_TRAILING)


_OWN_BASE_NORM = _normalise(OWN_BASE)


def _is_own(url: str) -> bool:
    """True for the pack's own site: the base itself, or anything under it.

    Matching on the base plus a separator rather than on the bare prefix, so a lookalike host path
    (`.../aisafety-radar-mirror/x`) does not inherit the allowance.
    """
    return url == _OWN_BASE_NORM or url.startswith(_OWN_BASE_NORM + "/")


def gate_provenance(rendered: dict[str, str], items: list[Item]) -> GateResult:
    """Every URL and `[P<id>]` reference in the rendered files traces back to a source."""
    allowed = {_normalise(i.url) for i in items}
    for i in items:
        allowed.update(_normalise(u) for u in _URL_RE.findall(i.markdown))
    ids = {i.id for i in items}
    details: list[str] = []
    for name, text in rendered.items():
        for url in _URL_RE.findall(text):
            u = _normalise(url)
            if u not in allowed and not _is_own(u):
                details.append(f"{name}: URL not in any source: {u}")
        for pid in _PID_RE.findall(text):
            if pid not in ids:
                details.append(f"{name}: unresolved item reference [P{pid}]")
    return GateResult(name="provenance", passed=not details, blocking=True, details=details)


def gate_budgets(
    rendered: dict[str, str],
    settings: Settings,
    counter: Callable[[str], int],
    expected_items: dict[str, int],
) -> GateResult:
    """Each file inside its token budget, and rendering as many items as the run selected."""
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
    prefix = f"<!-- aisafety-radar · version {version}"
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
