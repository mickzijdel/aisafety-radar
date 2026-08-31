"""The on-disk archive of what each day's run fetched, extracted and reported.

One directory per day (`root/YYYY/MM/DD`) holding JSONL for items, comments and extractions plus a
`run.json` report; the ledger and seen-ids stores sit at the root because they span days.

Every write merges with what is already on disk, keyed by id, so re-running a day overwrites the
rows it re-produces and leaves the rest alone (spec 6.9). Appending instead would double every row
on the second run of a day, and truncating would lose the sources that failed on the rerun.

Writes go through the ledger's `_atomic_write`, so an interrupted run leaves the previous day's file
intact rather than a half-written one that the next read would choke on.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from pipeline.ledger import _atomic_write
from pipeline.models import Comment, Item
from pipeline.stages.verify import ExtractionRecord

T = TypeVar("T", bound=BaseModel)


class Archive:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def ledger_path(self) -> Path:
        return self.root / "posts-ledger.json"

    @property
    def seen_path(self) -> Path:
        return self.root / "seen-ids.json"

    def day_dir(self, day: date) -> Path:
        return self.root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"

    def _merge_jsonl(
        self, path: Path, rows: Iterable[BaseModel], key: Callable[[dict[str, Any]], str]
    ) -> None:
        """Write `rows` into `path`, replacing any row already there with the same key.

        Insertion order is the order rows first appeared, so a rerun keeps the day's existing
        rows where they were and appends only what is genuinely new.
        """
        existing: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    obj = json.loads(line)
                    existing[key(obj)] = obj
        for row in rows:
            obj = row.model_dump(mode="json")
            existing[key(obj)] = obj
        _atomic_write(path, "".join(json.dumps(o) + "\n" for o in existing.values()))

    def _read_jsonl(self, path: Path, model: type[T]) -> list[T]:
        if not path.exists():
            return []
        return [
            model.model_validate(json.loads(line))
            for line in path.read_text().splitlines()
            if line.strip()
        ]

    def write_items(self, day: date, items: Iterable[Item]) -> None:
        self._merge_jsonl(self.day_dir(day) / "items.jsonl", items, lambda o: o["id"])

    def read_items(self, day: date) -> list[Item]:
        return self._read_jsonl(self.day_dir(day) / "items.jsonl", Item)

    def write_extractions(self, day: date, records: Iterable[ExtractionRecord]) -> None:
        self._merge_jsonl(
            self.day_dir(day) / "extracted.jsonl", records, lambda o: o["extraction"]["item_id"]
        )

    def read_extractions(self, day: date) -> list[ExtractionRecord]:
        return self._read_jsonl(self.day_dir(day) / "extracted.jsonl", ExtractionRecord)

    def update_extraction(self, day: date, record: ExtractionRecord) -> None:
        """Replace one day's record for `record.extraction.item_id`, leaving the rest alone."""
        self.write_extractions(day, [record])

    def write_comments(self, day: date, comments: Iterable[Comment]) -> None:
        self._merge_jsonl(self.day_dir(day) / "comments.jsonl", comments, lambda o: o["id"])

    def write_run(self, day: date, payload: dict[str, Any]) -> None:
        """Write the day's run report: statuses, usage, gate results, version.

        Unlike the JSONL writes this replaces the file outright -- the report describes one run,
        and merging two runs' reports would describe neither. `default=str` keeps a stray date or
        Path from sinking the report at the last step of a run.
        """
        _atomic_write(self.day_dir(day) / "run.json", json.dumps(payload, indent=1, default=str))

    def days_with_extractions(self, before: date, n: int) -> list[date]:
        """The days in the `n` days *preceding* `before` that have an extractions file."""
        days = []
        for offset in range(1, n + 1):
            d = before - timedelta(days=offset)
            if (self.day_dir(d) / "extracted.jsonl").exists():
                days.append(d)
        return sorted(days)

    def find_extraction(
        self, item_id: str, day: date, lookback_days: int = 30
    ) -> tuple[date, ExtractionRecord] | None:
        """The most recent record for `item_id` on or before `day`, with the day it was found on.

        A post can be extracted once and then resurface days later on comment movement; the digest
        reuses that extraction rather than paying for it again, so the search walks backwards from
        `day` and stops at the first hit.
        """
        for offset in range(0, lookback_days + 1):
            d = day - timedelta(days=offset)
            for rec in self.read_extractions(d):
                if rec.extraction.item_id == item_id:
                    return d, rec
        return None
