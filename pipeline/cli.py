"""The `radar` command line: `run` for a day's pack, `probe` for a look at the live sources.

`run --dry-run --fixtures tests/fixtures/forummagnum` is the offline end-to-end path: recorded
payloads instead of the network, `CannedLLM` instead of the API. Nothing but the real `run` needs
an API key, and `anthropic` is imported only on that branch so a dry run works without one.

Rendering is not its own subcommand: a pack is rendered from what the run's own stages produced,
so there is nothing to render without a run.

`run` exits 0 when the pack published and `BLOCKED_EXIT` when a gate withheld it; `probe` exits 0
whatever the sources did, because its output *is* the report.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pipeline
from pipeline.archive import Archive
from pipeline.config import load_settings, user_agent
from pipeline.fetchers.base import Window, run_all
from pipeline.fetchers.fixtures import FixtureClient
from pipeline.fetchers.forummagnum import ForumClient
from pipeline.fetchers.forums import MIN_ITEMS_7D, SOURCE_NAMES, ForumFetcher
from pipeline.llm import LLM, CannedLLM
from pipeline.run import RunInputs, run_daily
from pipeline.versioning import next_letter, pack_version

# A blocked run is not a crashed one, and argparse already owns 2 for a usage error, so the two
# must not share an exit code: the workflow has to tell "the pack was withheld by a gate" from
# "the command line was wrong".
BLOCKED_EXIT = 3


def _clients(fixtures: Path | None, ua: str):
    """One client per site: recorded payloads when `fixtures` is set, the live API otherwise."""
    if fixtures:
        return {site: FixtureClient(site, fixtures) for site in SOURCE_NAMES}
    return {site: ForumClient(site, ua) for site in SOURCE_NAMES}


def _fetchers(clients):
    return [
        ForumFetcher(site, clients[site], name, MIN_ITEMS_7D[site])
        for site, name in SOURCE_NAMES.items()
    ]


def _existing_versions(changelog: Path) -> list[str]:
    """The pack versions the changelog already records, as `## <version> (<day>)` headings."""
    if not changelog.exists():
        return []
    return [
        line.split()[1]
        for line in changelog.read_text().splitlines()
        if line.startswith("## ") and len(line.split()) > 1
    ]


def cmd_run(args) -> int:
    settings = load_settings()
    fixtures = Path(args.fixtures) if args.fixtures else None
    clients = _clients(fixtures, user_agent(pipeline.__version__))
    day = (
        date.fromisoformat(args.day)
        if args.day
        # Fixtures date the pack from what was recorded, so a dry run is the same run every time.
        else (FixtureClient.day_of(fixtures) if fixtures else datetime.now(UTC).date())
    )
    if args.dry_run:
        llm = CannedLLM()
    else:
        import anthropic

        llm = LLM(anthropic.Anthropic(), settings)
    version = args.version or pack_version(
        day, next_letter(_existing_versions(Path(args.changelog)), day)
    )
    report = run_daily(
        RunInputs(
            settings=settings,
            llm=llm,
            archive=Archive(Path(args.data)),
            clients=clients,
            fetchers=_fetchers(clients),
            day=day,
            version=version,
            out_dir=Path(args.out),
        )
    )
    print(report.model_dump_json(indent=1))
    return 0 if report.published else BLOCKED_EXIT


def cmd_probe(args) -> int:
    """Fetch from every live source and print a status table. Never fails the job.

    The point is to see which sources answer, so a source that raises has to show up as a row
    rather than as a traceback; `run_all` already turns each failure into a not-ok status.
    """
    clients = _clients(None, user_agent(pipeline.__version__))
    window = Window.for_day(datetime.now(UTC).date(), hours=args.hours)
    result = run_all(_fetchers(clients), window)
    print("| source | ok | items | error |\n|---|---|---|---|")
    for status in result.statuses:
        print(
            f"| {status.source} | {'yes' if status.ok else 'NO'} | "
            f"{status.items} | {status.error or ''} |"
        )
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="radar")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="fetch, extract, gate and render one day's pack")
    run.add_argument("--day", help="YYYY-MM-DD; defaults to today, or to the fixtures' own day")
    run.add_argument("--data", required=True, help="archive root")
    run.add_argument("--out", required=True, help="where the rendered pack is written")
    run.add_argument("--version", help="pack version; derived from the changelog when omitted")
    run.add_argument("--changelog", default="CHANGELOG.md")
    run.add_argument("--dry-run", action="store_true", help="use the canned model, no API key")
    run.add_argument("--fixtures", help="replay recorded payloads instead of the network")
    run.set_defaults(fn=cmd_run)

    probe = sub.add_parser("probe", help="fetch from every live source and print a status table")
    probe.add_argument("--hours", type=int, default=168, help="window size, in hours")
    probe.set_defaults(fn=cmd_probe)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
