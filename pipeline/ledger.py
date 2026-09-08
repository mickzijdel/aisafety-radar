"""State that carries between daily runs: the seen-ids store and the daily posts ledger.

The ledger holds one map of `LedgerEntry` per day. A post that was seen on an earlier day but is
absent today keeps its last entry copied forward with `carried=True`, so the day's map is always
the full set of live posts rather than only today's fetch. `movements` diffs a day against the
previous one to find what gained comments or score.

Two bounds, and both are load-bearing, because this file is rewritten and pushed to the public
data repo every morning:

* `keep_days` prunes whole days off the back of the ledger.
* `window_days` bounds what a day *contains*. The carry-forward only carries a post still inside
  the live-post window; anything older can no longer move the digest, since the daily window fetch
  does not ask for it either. Without that bound the carry-forward is a ratchet - every post the
  pipeline has ever seen, copied into every later day, for ever. Simulated at 44 posts a day it
  reached 1,320 entries by day 30 and 11,340 by day 365, which is a file that eventually fails
  GitHub's 100 MB limit and, long before that, a diff nobody can read.

The bound is on the carry-forward alone. An item this run actually fetched is recorded whatever
its age, so nothing the run has in hand is ever thrown away here.
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

# How far back the live-post window reaches. It is the ledger's because two things have to agree
# about it: `window_post_ids`, and the carry-forward that decides what a day still holds.
# `pipeline.run` imports it rather than restating it, so the window fetch asks for exactly the
# span the ledger will keep.
WINDOW_DAYS = 14


def _window_cutoff(day: date, days: int) -> datetime:
    """Naive midnight, `days` days before `day`: the oldest `published_at` still in the window.

    The offset is dropped on both sides and the timestamp read as written, so this is a
    wall-clock comparison rather than a timezone-correct one. Deliberate: the window is a coarse
    recency filter, and a few hours either side of the cutoff does not change what it selects.
    Stripping the offset is also what keeps a tz-aware `published_at` from raising against the
    naive midnight cutoff.
    """
    return datetime.combine(day - timedelta(days=days), datetime.min.time())


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

    def __init__(self, path: Path, keep_days: int = 30, window_days: int = WINDOW_DAYS) -> None:
        self.path = path
        self.keep_days = keep_days
        self.window_days = window_days
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
        """That day's post map, as a copy: mutating the result must not touch the ledger."""
        return dict(self._days.get(day, {}))

    def latest_before(self, day: date) -> date | None:
        earlier = [d for d in self._days if d < day]
        return max(earlier) if earlier else None

    def snapshot(
        self, day: date, items: Iterable[Item], last_commented: dict[str, datetime | None]
    ) -> None:
        """Record `day` from today's `items`, carrying forward posts absent from this fetch.

        The day is rebuilt from scratch, so re-running the same day replaces it rather than
        merging with a half-finished earlier attempt.

        `last_commented` only covers the posts the comment refresh looked at (the 14-day window),
        so a missing key means "no news", not "no comments ever": a refetched post keeps the
        timestamp the previous day recorded. An explicit None is treated the same as an absent
        key for that reason. Only a real datetime overwrites what is already known.

        Only posts still inside the `window_days` window are carried forward. A post published
        before the cutoff cannot become a mover -- `movements` compares against yesterday, and the
        daily window fetch stopped asking for it -- so carrying it grows the file without ever
        changing what the run does. Today's own `items` are recorded whatever their age: the bound
        is on the carry-forward, never on what the run actually fetched.
        """
        today: dict[str, LedgerEntry] = {}
        prev_day = self.latest_before(day)
        if prev_day is not None:
            cutoff = _window_cutoff(day, self.window_days)
            for pid, e in self._days[prev_day].items():
                if e.published_at.replace(tzinfo=None) < cutoff:
                    continue
                today[pid] = e.model_copy(update={"carried": True})
        for item in items:
            # `today` still holds the carried entry for this post, if there was one.
            known = today.get(item.id)
            commented_at = last_commented.get(item.id)
            if commented_at is None and known is not None:
                commented_at = known.last_commented_at
            today[item.id] = LedgerEntry(
                post_id=item.id,
                url=item.url,
                title=item.title,
                published_at=item.published_at,
                score=item.score,
                agreement=item.agreement,
                comment_count=item.comment_count,
                last_commented_at=commented_at,
            )
        self._days[day] = today
        cutoff = day - timedelta(days=self.keep_days - 1)
        for d in [d for d in self._days if d < cutoff]:
            del self._days[d]

    def window_post_ids(self, day: date, days: int = WINDOW_DAYS) -> set[str]:
        """Ids of `day`'s posts published within the last `days` days.

        Shares `_window_cutoff` with the carry-forward in `snapshot`, so the two cannot disagree
        about what "in the window" means. See that helper for why the comparison is wall-clock.
        """
        cutoff = _window_cutoff(day, days)
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
