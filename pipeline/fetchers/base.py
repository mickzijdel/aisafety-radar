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
    def for_day(cls, day: date, hours: int = 24) -> Window:
        """The run's window: everything published in the `hours` before 05:40 UTC on `day`."""
        end = datetime.combine(day, time(5, 40), tzinfo=UTC)
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
