"""One day's run, from the fetchers to the rendered pack.

Every module below this one is a stage that knows nothing about the others; this is the file that
knows the order, and the order carries most of the correctness. Four decisions in it are worth
stating, because each is easy to get subtly wrong and cheap to get right here:

* **The gates take different item lists.** `gate_provenance` gets today's rendered posts *plus*
  the 14-day window posts, because a discussion mover is rendered from a ledger entry built out of
  that window and its link would otherwise read as model-invented. `gate_freshness` gets only
  today's rendered posts: a mover is an older post by definition, so handing it the same list would
  fail the window check on the very thing that made the post a mover.
* **The anomaly medians are raw fetcher counts.** `run.json` records `statuses[].items`, which is
  what each source returned *before* the floor, and that is the unit `apply_floor` compares
  against. Feeding it post-floor survivors would hide a flood the per-author cap already absorbed.
* **The scan gate does not cover movers.** `gate_scan` is given this run's rendered item ids, and
  a mover is an archived record from an earlier day republished through `refresh_comments`. That
  stage therefore scans its own refreshed signal and refuses to touch a record an earlier scan
  flagged; nothing here would catch either case.
* **Failure isolation is per source and per item.** `run_all` already isolates a daily fetch; the
  14-day window fetch, the comment refresh and every item's own extract/scan/judge are isolated
  here on the same principle, so one forum being down or one API call failing costs its own
  contribution and nothing else. The extract/scan/judge loop is where a run's money is spent, and
  a failure on the last item of it must not throw away everything already paid for. The refresh
  has no per-post isolation inside it, so a failure there costs the day's movers, not the run.
* **Files and seen ids are written last, and only if no blocking gate failed.** The archive, the
  ledger and the run report are written either way -- a run that could not publish is still a run
  that happened, and the next day's medians and cost comparison read those files -- but an item is
  only "seen" once the gates have passed, or a blocked run would spend the day's items without
  showing them to anyone and leave the rerun nothing to publish.

  Note precisely where that is in the pipeline: the gates pass in the `generate` job, one job
  *before* `promote` moves `main`, so "seen" means "cleared for publication", not "published". A
  day whose generation succeeded and whose promotion then failed has already spent its items, and
  regenerating it needs `ignore_seen` (`radar run --ignore-seen`). Re-running the failed `promote`
  job is the cheaper recovery, because the pack itself is intact on the `generated` branch.

Note that `archive.write_comments` is not called: phase 2 fetches comments only inside the refresh,
which writes back extractions rather than comment rows. The comment archive is phase 3's, when the
digest starts quoting threads directly.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from pipeline.archive import Archive
from pipeline.config import Settings
from pipeline.fetchers.base import Fetcher, Window, run_all
from pipeline.fetchers.forummagnum import ForumClient, Site
from pipeline.fetchers.forums import raw_post_to_item
from pipeline.floor import apply_floor
from pipeline.ledger import PostsLedger, SeenIds
from pipeline.llm import LLM
from pipeline.models import GateResult, Item, SourceStatus, Usage
from pipeline.qa.gates import (
    blocking_failures,
    gate_budgets,
    gate_cost,
    gate_freshness,
    gate_judge,
    gate_provenance,
    gate_scan,
    gate_source_minimums,
)
from pipeline.render import PackMeta, build_render_items, render_all
from pipeline.stages.comments import Mover, refresh_comments
from pipeline.stages.extract import extract_verified
from pipeline.stages.scan import (
    QUARANTINE_NOTE,
    JudgeResult,
    ScanResult,
    judge_record,
    needs_judge,
    scan_record,
)
from pipeline.stages.triage import select, triage
from pipeline.stages.verify import ExtractionRecord

log = logging.getLogger(__name__)

FORUMS_FILE = "31-today-forums.md"

# How far back the ledger's live-post window reaches, and the page size the window fetch asks for.
# The window is what makes a mover possible: a post outside it can no longer move the digest.
WINDOW_DAYS = 14
WINDOW_LIMIT = 500


@dataclass
class RunInputs:
    settings: Settings
    llm: LLM
    archive: Archive
    # `FixtureClient` satisfies the same three methods without inheriting from `ForumClient`; the
    # annotation names the real thing because that is what production passes.
    clients: dict[Site, ForumClient] | None
    fetchers: Sequence[Fetcher]
    day: date
    version: str
    out_dir: Path
    rng_seed: int = 0
    # Regenerate a day whose items are already spent. Off by default, because the filter is what
    # stops a rerun republishing yesterday; see the docstring's note on the seen ids.
    ignore_seen: bool = False


class RunReport(BaseModel):
    """What the day did, as written to `run.json` and printed by the CLI.

    `dropped`, `scan_flagged` and `failed` exist so a shrunken pack is never silent: an item can
    leave the render set without failing a gate, and a count that only ever appears in a log line
    is a count nobody reads. `dropped` is the union -- every selected item that never reached the
    render set -- and the other two say why.
    """

    model_config = ConfigDict(extra="forbid")
    version: str
    statuses: list[SourceStatus]
    items_fetched: int
    items_after_floor: int
    items_selected: int
    rendered_files: list[str]
    gates: list[GateResult]
    usage: Usage
    movers: int
    published: bool
    dropped: list[str] = Field(default_factory=list)
    scan_flagged: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


def _source_medians(archive: Archive, day: date) -> dict[str, int]:
    """Each source's median RAW fetched items per day over the last 30 recorded runs.

    Raw, because `statuses[].items` is what the fetcher returned before any floor ran, which is
    the unit `apply_floor`'s anomaly check is defined in. A source with fewer than seven days of
    history has no median: too little to call anything anomalous against.
    """
    counts: dict[str, list[int]] = {}
    for report in archive.recent_runs(day, 30):
        for status in report.get("statuses", []):
            counts.setdefault(status["source"], []).append(int(status["items"]))
    return {source: int(median(v)) for source, v in counts.items() if len(v) >= 7}


def _trailing_cost_median(archive: Archive, day: date) -> float | None:
    """The median cost of the last 14 recorded runs, or None with fewer than five to go on."""
    costs = [float(r.get("usage", {}).get("cost_usd", 0)) for r in archive.recent_runs(day, 14)]
    return median(costs) if len(costs) >= 5 else None


def _window_posts(
    clients: dict[Site, ForumClient], day: date
) -> tuple[list[Item], dict[str, datetime | None]]:
    """The last `WINDOW_DAYS` of posts per site, and when each was last commented on.

    Isolated per site, and all or nothing within one: this fetch feeds the ledger and the movers,
    so one forum being unreachable -- or sending a record that will not convert -- must cost that
    forum's movers rather than the whole run, and must not leave the ledger holding half a site.
    """
    items: list[Item] = []
    last_commented: dict[str, datetime | None] = {}
    for site, client in clients.items():
        try:
            posts = client.posts_since(
                day - timedelta(days=WINDOW_DAYS),
                view="new",
                limit=WINDOW_LIMIT,
                # The AF is an AI-safety forum end to end and carries no AI tag to filter on.
                require_ai_tag=site != "af",
            )
            converted = [(raw_post_to_item(site, p), p.lastCommentedAt) for p in posts]
        except Exception:  # noqa: BLE001 - isolation is the point
            log.exception("%s: 14-day window fetch failed; no movers from this site today", site)
            continue
        for item, commented_at in converted:
            items.append(item)
            last_commented[item.id] = commented_at
    return items, last_commented


def run_daily(inputs: RunInputs) -> RunReport:
    settings, llm, archive, day = inputs.settings, inputs.llm, inputs.archive, inputs.day
    window = Window.for_day(day)
    rng = random.Random(inputs.rng_seed)
    clients = inputs.clients or {}

    fetched = run_all(inputs.fetchers, window)
    seen = SeenIds(archive.seen_path).load()
    fresh = fetched.items if inputs.ignore_seen else [i for i in fetched.items if i.id not in seen]
    # Medians in raw fetched items per day, which is the unit `apply_floor` compares against.
    floor = apply_floor(fresh, settings.floor, day, _source_medians(archive, day))
    for note in floor.anomalies:
        log.warning("anomaly: %s", note)

    results = triage(floor.kept, llm, settings)
    selected = select(floor.kept, results, settings.caps.forums_per_day)

    records: dict[str, ExtractionRecord] = {}
    scans: dict[str, ScanResult] = {}
    judges: dict[str, JudgeResult] = {}
    quarantined: list[ExtractionRecord] = []
    failed: list[str] = []
    for item in selected:
        # Isolated per item, because this loop is where the run's money goes: one transient API
        # error on the fortieth of fifty items must cost that item, not the thirty-nine already
        # paid for and not the day's pack.
        try:
            record = extract_verified(item, llm, settings)
            scan = scan_record(record, llm, settings)
            scans[item.id] = scan
            if scan.flagged:
                log.warning("injection scan flagged %s: %s", item.id, scan.reason)
                # Out of `records`, so out of the pack, but archived with the reason: spec 6.8
                # gate 4 wants the flagged text kept for review, and a flag whose text nobody can
                # go back and read teaches nothing.
                quarantined.append(
                    record.model_copy(
                        update={"notes": record.notes + [f"{QUARANTINE_NOTE} {scan.reason}"]}
                    )
                )
                continue
            if needs_judge(record, rng, settings):
                verdict = judge_record(record, item, llm, settings)
                judges[item.id] = verdict
                if not verdict.passed and record.extraction.importance >= 4:
                    record = record.model_copy(
                        update={
                            "degraded": True,
                            "notes": record.notes + [f"judge: {verdict.notes}"],
                        }
                    )
            records[item.id] = record
        except Exception:  # noqa: BLE001 - one item is not the run
            log.exception("item %s failed during extraction, scan or judging; skipping it", item.id)
            failed.append(item.id)
    # An item selected but never recorded is one `build_render_items` would drop in silence. That
    # is the scan-flagged set plus the failed one; all three are reported, so a pack that shrank
    # says why and a new cause would show up rather than passing as one of these two.
    dropped = [i.id for i in selected if i.id not in records]
    rendered_items = [i for i in selected if i.id in records]
    assert judges.keys() <= records.keys(), "judged an item that never reached the render set"

    ledger = PostsLedger(archive.ledger_path).load()
    window_items, last_commented = _window_posts(clients, day)
    ledger.snapshot(day, rendered_items + window_items, last_commented)
    movers: list[Mover] = []
    if clients:
        try:
            movers = refresh_comments(clients, ledger, archive, llm, settings, day)
        except Exception:  # noqa: BLE001 - a lost refresh is not a lost run
            log.exception("comment refresh failed; publishing without discussion movers")

    meta = PackMeta(version=inputs.version, generated_at=datetime.now(UTC), data_through=window.end)
    rendered = render_all(
        meta,
        build_render_items(rendered_items, list(records.values())),
        movers,
        fetched.statuses,
        llm.count_tokens,
    )
    assert FORUMS_FILE in rendered, f"{FORUMS_FILE} was not rendered; the gates would pass on air"

    gates = [
        # Movers are rendered from ledger entries built out of the window posts, so provenance
        # needs those items too or a mover's own link reads as unsourced. Their new top comment's
        # author is fetched source text that belongs to no item at all, so it goes in separately.
        gate_provenance(
            rendered,
            rendered_items + window_items,
            source_text=[m.top_new_comment.author for m in movers if m.top_new_comment],
        ),
        # Both posts and movers render as `- **` lines; the index's are recipes and are not counted.
        gate_budgets(
            rendered,
            settings,
            llm.count_tokens,
            {FORUMS_FILE: len(rendered_items) + len(movers)},
        ),
        # Today's posts only: a mover is older than the window by definition.
        gate_freshness(rendered, rendered_items, window.start, window.end, inputs.version),
        gate_scan(scans, {i.id for i in rendered_items}),
        gate_judge(judges, records),
        gate_cost(
            llm.usage,
            _trailing_cost_median(archive, day),
            settings.thresholds.cost_alert_multiplier,
        ),
        # Reads the last seven run reports, not this run: a fetcher that quietly stopped matching
        # anything is indistinguishable from a quiet Tuesday inside one day's counts.
        gate_source_minimums(archive, day, inputs.fetchers),
    ]
    published = not blocking_failures(gates)

    archive.write_items(day, fetched.items)
    # The quarantined records go to the same file: it is the day's record of what the model wrote,
    # not of what was published, and each one says in its notes why it went no further.
    archive.write_extractions(day, list(records.values()) + quarantined)
    ledger.save()
    report = RunReport(
        version=inputs.version,
        statuses=fetched.statuses,
        items_fetched=len(fetched.items),
        items_after_floor=len(floor.kept),
        items_selected=len(selected),
        rendered_files=list(rendered),
        gates=gates,
        usage=llm.usage,
        movers=len(movers),
        published=published,
        dropped=dropped,
        scan_flagged=[iid for iid, scan in scans.items() if scan.flagged],
        failed=failed,
    )
    archive.write_run(day, report.model_dump(mode="json"))
    if published:
        # "Seen" means the gates passed, not fetched. A run a gate blocked has shown nobody
        # anything, and marking its items seen would make that failure permanent: the rerun that
        # fixed the problem would find every item already spent and publish an empty pack. The
        # archive and the ledger above are written either way, because the day happened either
        # way. Note that this is written whether or not `ignore_seen` skipped the filter: the flag
        # is for regenerating one day, not for turning the ledger off.
        for item in fetched.items:
            seen.add(item.id)
        seen.save()
        inputs.out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in rendered.items():
            (inputs.out_dir / name).write_text(text)
    else:
        log.error(
            "not published; blocking gates failed: %s",
            [f"{g.name}: {g.details}" for g in blocking_failures(gates)],
        )
    return report
