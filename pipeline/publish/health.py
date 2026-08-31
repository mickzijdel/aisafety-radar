"""What the workflow says about a run: the pinned health issue, the guard, the dead-man's ping.

Three jobs, one report file:

* `render_issue_body` turns `run.json` into the body of one pinned issue. It is written for a
  human skimming a phone notification, so the counts that explain a *shrunken* pack -- `dropped`,
  `scan_flagged`, `failed` -- get their own table with the ids in it. A count with no ids is a
  number nobody can act on.
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
    lines += ["", "## Gates", "", "| gate | passed | blocking | details |", "|---|---|---|---|"]
    for gate in report.get("gates", []):
        lines.append(
            # `_yes` shouts NO, which is right for a failure and wrong for "this gate is
            # advisory", so `blocking` renders plainly.
            f"| {_cell(gate['name'])} | {_yes(gate['passed'])} | "
            f"{'yes' if gate['blocking'] else 'no'} | "
            f"{_cell('; '.join(gate.get('details') or []))} |"
        )
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

    Two failures publish an *empty* pack rather than no pack, and neither trips a gate: a same-day
    rerun whose items are all already seen, and a systematic per-item failure that leaves every
    selected item in `failed`. Both look like a successful run from the outside -- the gates pass
    on an empty file -- so the arithmetic is checked here before anything reaches the `generated`
    branch.
    """
    if not report:
        return "no report.json: the run crashed before it could write one"
    if not report.get("published"):
        return "the run did not publish: a blocking gate withheld the pack"
    rendered = int(report.get("items_selected", 0)) - len(report.get("failed") or [])
    if rendered <= 0:
        return (
            f"the run published {rendered} rendered items "
            f"({report.get('items_selected', 0)} selected, "
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
