"""State that carries between daily runs: the seen-ids store and the daily posts ledger.

The ledger holds one map of `LedgerEntry` per day. A post that was seen on an earlier day but is
absent today keeps its last entry copied forward with `carried=True`, so the day's map is always
the full set of live posts rather than only today's fetch. `movements` diffs a day against the
previous one to find what gained comments or score.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from pipeline.models import Item


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file and a rename.

    A crash or a full disk mid-write must never leave a half-written ledger behind: the previous
    file stays intact until `os.replace` swaps the complete one in, and the temp file is removed
    if anything goes wrong.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class SeenIds:
    """The set of item ids already published, as a JSON list on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ids: set[str] = set()

    def load(self) -> SeenIds:
        if self.path.exists():
            self._ids = set(json.loads(self.path.read_text()))
        return self

    def __contains__(self, item_id: str) -> bool:
        return item_id in self._ids

    def add(self, item_id: str) -> None:
        self._ids.add(item_id)

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(sorted(self._ids)))


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: str
    url: str
    title: str
    published_at: datetime
    score: int | None
    agreement: int | None
    comment_count: int
    last_commented_at: datetime | None
    carried: bool = False


@dataclass
class Movement:
    post_id: str
    new_comments: int
    score_before: int | None
    score_after: int | None


class PostsLedger:
    """Per-day maps of post state, pruned to the last `keep_days` days."""

    def __init__(self, path: Path, keep_days: int = 30) -> None:
        self.path = path
        self.keep_days = keep_days
        self._days: dict[date, dict[str, LedgerEntry]] = {}

    def load(self) -> PostsLedger:
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self._days = {
                date.fromisoformat(d): {
                    pid: LedgerEntry.model_validate(e) for pid, e in posts.items()
                }
                for d, posts in raw["days"].items()
            }
        return self

    def save(self) -> None:
        payload = {
            "days": {
                d.isoformat(): {pid: e.model_dump(mode="json") for pid, e in posts.items()}
                for d, posts in sorted(self._days.items())
            }
        }
        _atomic_write(self.path, json.dumps(payload, indent=1))

    def days(self) -> list[date]:
        return sorted(self._days)

    def entries(self, day: date) -> dict[str, LedgerEntry]:
        return self._days.get(day, {})

    def latest_before(self, day: date) -> date | None:
        earlier = [d for d in self._days if d < day]
        return max(earlier) if earlier else None

    def snapshot(
        self, day: date, items: Iterable[Item], last_commented: dict[str, datetime | None]
    ) -> None:
        """Record `day` from today's `items`, carrying forward posts absent from this fetch.

        The day is rebuilt from scratch, so re-running the same day replaces it rather than
        merging with a half-finished earlier attempt.
        """
        today: dict[str, LedgerEntry] = {}
        prev_day = self.latest_before(day)
        if prev_day is not None:
            for pid, e in self._days[prev_day].items():
                today[pid] = e.model_copy(update={"carried": True})
        for item in items:
            today[item.id] = LedgerEntry(
                post_id=item.id,
                url=item.url,
                title=item.title,
                published_at=item.published_at,
                score=item.score,
                agreement=item.agreement,
                comment_count=item.comment_count,
                last_commented_at=last_commented.get(item.id),
            )
        self._days[day] = today
        cutoff = day - timedelta(days=self.keep_days - 1)
        for d in [d for d in self._days if d < cutoff]:
            del self._days[d]

    def window_post_ids(self, day: date, days: int = 14) -> set[str]:
        """Ids of `day`'s posts published within the last `days` days.

        The offset is dropped and the timestamp read as written, so this is a wall-clock
        comparison rather than a timezone-correct one. Deliberate: the window is a coarse
        recency filter, and a few hours either side of the cutoff does not change what it
        selects. Stripping the offset is also what keeps a tz-aware `published_at` from
        raising against the naive midnight cutoff.
        """
        cutoff = datetime.combine(day - timedelta(days=days), datetime.min.time())
        return {
            pid
            for pid, e in self.entries(day).items()
            if e.published_at.replace(tzinfo=None) >= cutoff
        }

    def movements(self, day: date) -> list[Movement]:
        """What changed on `day` against the previous recorded day.

        Only posts actually fetched on `day` can move; a carried entry is stale by definition.
        A post whose comments and score both held steady is not a movement.
        """
        prev_day = self.latest_before(day)
        if prev_day is None:
            return []
        prev = self._days[prev_day]
        moves: list[Movement] = []
        for pid, e in self.entries(day).items():
            if e.carried:
                continue
            before = prev.get(pid)
            new_comments = e.comment_count - (before.comment_count if before else 0)
            score_before = before.score if before else None
            if before is None or new_comments != 0 or score_before != e.score:
                moves.append(Movement(pid, new_comments, score_before, e.score))
        return moves
