"""What the workflow says about a run: the pinned health issue, the guard, the dead-man's ping.

Three jobs, one report file:

* `render_issue_body` turns `run.json` into the body of one pinned issue. It is written for a
  human skimming a phone notification, so the counts that explain a *shrunken* pack -- `dropped`,
  `scan_flagged`, `failed` -- get their own table with the ids in it. A count with no ids is a
  number nobody can act on. The 7-day source minimums get a table of their own for the same
  reason: that gate never blocks, so its failure reaches nobody unless the issue says it plainly.
  So do the 14-day window fetch and the comment refresh: neither reaches a gate, and both fail
  into a mover count of zero, which is also the correct value on day one and on a quiet day.
* `refusal_reason` is the promote guard. It lives here, next to the issue body, because both read
  the same report and must agree about what a bad run looks like; it is a function rather than
  four lines of YAML so it can be tested.
* `ping` is the Healthchecks dead-man's switch, and it may never fail the run: the point of the
  switch is that *silence* raises the alarm, so a failed ping must not turn a good day red.

Every third-party string that lands in a table cell goes through `_cell`. A fetcher error is
remote text, and a `|` in it would otherwise shift every column of the row.
"""

from __future__ import annotations

import logging
import subprocess

import httpx

from pipeline.qa.gates import SOURCE_MINIMUMS_GATE

LABEL = "radar-health"

log = logging.getLogger(__name__)

# Ids and gate details can be long; an issue body has a size limit and a reader has a smaller one.
_CELL_LIMIT = 300


def _cell(text: str) -> str:
    """`text` as one table cell: single line, pipes escaped, truncated."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= _CELL_LIMIT else flat[: _CELL_LIMIT - 1] + "…"


def _yes(value: object) -> str:
    return "yes" if value else "NO"


def _count_row(name: str, ids: list[str]) -> str:
    return f"| {name} | {len(ids)} | {_cell(', '.join(ids))} |"


def _minimum_rows(report: dict) -> list[str]:
    """The `source-minimums` gate as its own section: one row per source or category in trouble.

    It gets a section of its own rather than living in its `details` cell in the gates table
    because it is the only non-blocking gate whose failure needs a human to *do* something: the
    pack published fine, and the news is that a fetcher has probably stopped matching anything.
    Buried in a truncated cell next to five passing gates, that is a line nobody reads.

    A *failing* gate's detail is `"<name>: <what went wrong>"`, which splits into the two
    columns. A passing gate's detail is prose and is passed through whole: the only one the gate
    writes is the insufficient-history line, which carries a colon of its own, so splitting it
    would render "3 of the last 7 runs recorded | too little history to judge a week" -- a row
    that reads as a source and a shortfall to anyone skimming the section.
    """
    gate = next((g for g in report.get("gates", []) if g.get("name") == SOURCE_MINIMUMS_GATE), None)
    lines = ["", "## 7-day minimums", "", "| source | 7-day count |", "|---|---|"]
    if gate is None:
        return [*lines, "| (not checked) | this run recorded no 7-day minimum check |"]
    details = gate.get("details") or []
    if not details:
        return [*lines, "| every source and category | above its 7-day minimum |"]
    for detail in details:
        name, sep, rest = detail.partition(": ")
        split = sep and not gate.get("passed")
        lines.append(f"| {_cell(name)} | {_cell(rest)} |" if split else f"| {_cell(detail)} |  |")
    return lines


def _side_rows(report: dict) -> list[str]:
    """The per-site window fetch and the comment refresh, as their own section.

    They are not in the `Sources` table because they are not sources: `statuses` is the raw daily
    fetch, which is what the medians and the 7-day minimums are computed from. They get a section
    rather than a log line because neither reaches a gate, and both fail into "0 movers" - the
    same number a correct day one prints.
    """
    rows = list(report.get("window_statuses") or [])
    refresh = report.get("refresh_status")
    if refresh:
        rows.append(refresh)
    lines = [
        "",
        "## Window and refresh",
        "",
        "| source | ok | items | error |",
        "|---|---|---|---|",
    ]
    if not rows:
        return [*lines, "| (not recorded) | - | - | this run recorded no window or refresh |"]
    for row in rows:
        lines.append(
            f"| {_cell(row['source'])} | {_yes(row['ok'])} | {row['items']} | "
            f"{_cell(row.get('error') or '')} |"
        )
    return lines


def render_issue_body(report: dict, outcome: str | None = None, run_url: str | None = None) -> str:
    """The pinned issue's body for one run's report.

    `report` may be `{}`: a run that crashed before writing `run.json` still has to produce an
    issue saying so, which is exactly the run whose health matters most.
    """
    published = bool(report.get("published"))
    lines = [
        f"# Pipeline health · version {report.get('version') or 'unknown'}",
        "",
        f"Outcome: {outcome or ('published' if published else 'not published')}",
        f"Published: {_yes(published)}",
    ]
    if run_url:
        lines.append(f"Run: {run_url}")
    lines += ["", "## Sources", "", "| source | ok | items | error |", "|---|---|---|---|"]
    for status in report.get("statuses", []):
        lines.append(
            f"| {_cell(status['source'])} | {_yes(status['ok'])} | {status['items']} | "
            f"{_cell(status.get('error') or '')} |"
        )
    lines += _side_rows(report)
    lines += ["", "## Gates", "", "| gate | passed | blocking | details |", "|---|---|---|---|"]
    for gate in report.get("gates", []):
        lines.append(
            # `_yes` shouts NO, which is right for a failure and wrong for "this gate is
            # advisory", so `blocking` renders plainly.
            f"| {_cell(gate['name'])} | {_yes(gate['passed'])} | "
            f"{'yes' if gate['blocking'] else 'no'} | "
            f"{_cell('; '.join(gate.get('details') or []))} |"
        )
    lines += _minimum_rows(report)
    lines += [
        "",
        "## Items",
        "",
        f"Selected {report.get('items_selected', 0)} item(s) for extraction; "
        f"{report.get('movers', 0)} discussion mover(s).",
        "",
        "| bucket | count | ids |",
        "|---|---|---|",
        _count_row("dropped", report.get("dropped") or []),
        _count_row("scan-flagged", report.get("scan_flagged") or []),
        _count_row("failed", report.get("failed") or []),
        "",
        f"Cost this run: ${float(report.get('usage', {}).get('cost_usd', 0.0)):.2f}",
    ]
    return "\n".join(lines) + "\n"


def refusal_reason(report: dict) -> str | None:
    """Why this run's pack must not be published, or None when it may be.

    Three failures publish an *empty* pack rather than no pack, and none of them trips a gate: a
    same-day rerun whose items are all already seen, a systematic per-item failure that leaves
    every selected item in `failed`, and an injection-scan wave that quarantines every one of
    them. All three look like a successful run from the outside -- the gates pass on an empty
    file -- so the arithmetic is checked here before anything reaches the `generated` branch.

    The subtrahend is `dropped`, not `failed`. `RunReport.dropped` is the union of every selected
    item that never reached the render set; `failed` is only the exception branch of that union
    and misses the scan-flagged items, so subtracting it would let a full scan wave through as a
    healthy run.
    """
    if not report:
        return "no report.json: the run crashed before it could write one"
    if not report.get("published"):
        return "the run did not publish: a blocking gate withheld the pack"
    if "dropped" not in report:
        # `dropped` is the subtrahend below, so an absent key would read as zero drops and let
        # a report this guard cannot actually check through as a healthy run.
        return "the report carries no `dropped` list: cannot verify the pack is not empty"
    selected = int(report.get("items_selected", 0))
    dropped = report.get("dropped") or []
    rendered = selected - len(dropped)
    if rendered <= 0:
        return (
            f"the run published {rendered} rendered items ({selected} selected, "
            f"{len(dropped)} dropped: {len(report.get('scan_flagged') or [])} scan-flagged, "
            f"{len(report.get('failed') or [])} failed): refusing to promote an empty pack"
        )
    return None


def update_pinned_issue(repo: str, body: str, title: str = "Pipeline health") -> None:
    """Rewrite the open `radar-health` issue's body, opening one if there is none.

    One issue, edited in place, rather than one per run: a daily run that opened an issue a day
    would train its only reader to mute the repository.
    """
    number = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            LABEL,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[0].number",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if number:
        subprocess.run(["gh", "issue", "edit", number, "--repo", repo, "--body", body], check=True)
    else:
        subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--label",
                LABEL,
                "--body",
                body,
            ],
            check=True,
        )


def ping(url: str | None) -> None:
    """Dead-man's-switch ping. A failed ping must never fail the run, so log and move on."""
    if not url:
        return
    try:
        httpx.get(url, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("healthchecks ping failed: %s", exc)
