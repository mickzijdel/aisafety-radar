"""The fetcher contract, the daily window, and the runner that isolates a failing source."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

from pipeline.models import Category, Item, SourceStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Window:
    day: date
    start: datetime
    end: datetime

    @classmethod
    def for_day(cls, day: date, hours: int = 24, now: datetime | None = None) -> Window:
        """The run's window: everything published in the `hours` before `now`, or before 05:40.

        The 05:40 default is a *nominal* boundary, and the only callers that should still take it
        are the ones for which "now" means nothing: a fixture replay, a backfill of an earlier
        day, and the probe's arbitrary `--hours` lookback.

        A run happening now passes `now` and gets a window that ends when it actually fetched.
        The nominal boundary tiled exactly and the cron never reaches it - it fires at 05:23 and
        the fetch lands around 05:26 - so a post published at 05:30 fell in the gap: inside
        today's window, which had already been fetched, and before tomorrow's start of 05:40. It
        was never fetched, never marked seen, and disappeared silently while the pack header
        claimed data through 05:40Z. See `pipeline.run._run_window` for the overlap that goes
        with it.
        """
        end = now or datetime.combine(day, time(5, 40), tzinfo=UTC)
        return cls(day=day, start=end - timedelta(hours=hours), end=end)


class Fetcher(Protocol):
    name: str
    category: Category
    min_items_7d: int

    def fetch(self, window: Window) -> list[Item]: ...


@dataclass
class FetchResult:
    items: list[Item]
    statuses: list[SourceStatus]


def run_all(fetchers: Sequence[Fetcher], window: Window) -> FetchResult:
    """Fetch from every source, recording rather than propagating a source's failure.

    One dead source must not cost the day's whole run, so the exception becomes a not-ok
    `SourceStatus` the gates can see and the run carries on with the sources that worked.
    """
    items: list[Item] = []
    statuses: list[SourceStatus] = []
    for f in fetchers:
        try:
            got = f.fetch(window)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            log.exception("fetcher %s failed", f.name)
            statuses.append(
                SourceStatus(source=f.name, category=f.category, ok=False, items=0, error=str(exc))
            )
            continue
        items.extend(got)
        statuses.append(SourceStatus(source=f.name, category=f.category, ok=True, items=len(got)))
    return FetchResult(items=items, statuses=statuses)
