"""Entry floor and caps: the anti-flood defence that runs before any model call.

Every check here is cheap and local, so an item rejected at this stage costs nothing beyond the
fetch that already happened (spec 5.1). Order matters: per-item floors first, then the caps that
need to compare items against each other.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from pipeline.models import Comment, Item


class FloorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_karma_min: dict[str, int] = Field(default_factory=lambda: {"lw": 20, "eaf": 10, "af": 0})
    comment_karma_min: int = 3
    account_age_days: int = 30
    per_author_per_day: int = 3
    per_source_per_day: int = 150
    anomaly_multiplier: float = 3.0


@dataclass
class Rejection:
    item_id: str
    reason: str


@dataclass
class FloorResult:
    kept: list[Item]
    rejected: list[Rejection] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def _account_old_enough(created: datetime | None, today: date, days: int) -> bool:
    """An unknown creation date counts as too new: absent evidence is not evidence of standing."""
    if created is None:
        return False
    return created.date() <= today - timedelta(days=days)


def _by_score_then_id(item: Item) -> tuple[int, str]:
    """Rank key: highest score first, id as the tie-break.

    Ties broken by id rather than by input order so a rerun that fetches the same items in a
    different order keeps the same ones.
    """
    return (-(item.score or 0), item.id)


def apply_floor(
    items: list[Item],
    cfg: FloorConfig,
    today: date,
    source_medians: dict[str, int] | None = None,
) -> FloorResult:
    """Drop low-signal items and cap floods, returning what survives and why the rest did not.

    `source_medians` maps a source name to its recent per-day item count; a source that exceeds
    `anomaly_multiplier` times its median is capped to the median and reported in `anomalies`.
    """
    result = FloorResult(kept=[])

    # Per-item floors. Curated and frontpage posts are promoted by the forum's own moderation,
    # which is a stronger signal than either karma or account age, so they bypass both.
    survivors: list[Item] = []
    for item in items:
        bypass = item.curated or item.frontpage
        floor = cfg.post_karma_min.get(item.site or "", 0)
        if not bypass and (item.score or 0) < floor:
            result.rejected.append(Rejection(item.id, f"karma {item.score} below {floor}"))
            continue
        if not bypass and not _account_old_enough(
            item.author_created_at, today, cfg.account_age_days
        ):
            result.rejected.append(Rejection(item.id, "account younger than floor"))
            continue
        survivors.append(item)

    # Per-author cap, keyed by (source, author): one prolific poster cannot swamp a source, but
    # the same person crossposting to two forums is two separate allowances.
    by_author: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for item in survivors:
        by_author[(item.source, item.author)].append(item)
    after_author: list[Item] = []
    for group in by_author.values():
        group.sort(key=_by_score_then_id)
        after_author.extend(group[: cfg.per_author_per_day])
        for extra in group[cfg.per_author_per_day :]:
            result.rejected.append(Rejection(extra.id, "per-author cap"))

    # Per-source cap, tightened to the median when the day's volume looks anomalous.
    by_source: dict[str, list[Item]] = defaultdict(list)
    for item in after_author:
        by_source[item.source].append(item)
    for source, group in by_source.items():
        group.sort(key=_by_score_then_id)
        cap = cfg.per_source_per_day
        # A median of 0 (or a source with no history) gives nothing to compare against, and
        # capping to 0 would discard the source entirely — leave the flat cap in charge.
        median = (source_medians or {}).get(source)
        if median and len(group) > cfg.anomaly_multiplier * median:
            result.anomalies.append(
                f"{source}: {len(group)} items vs 30-day median {median}; capped to {median}"
            )
            cap = min(cap, median)
        result.kept.extend(group[:cap])
        for extra in group[cap:]:
            result.rejected.append(Rejection(extra.id, "per-source cap"))
    return result


def filter_comments(comments: list[Comment], cfg: FloorConfig, today: date) -> list[Comment]:
    """Apply the comment karma floor and the account-age floor, preserving input order.

    Comments carry no curated/frontpage promotion, so there is no bypass here.
    """
    return [
        c
        for c in comments
        if c.score >= cfg.comment_karma_min
        and _account_old_enough(c.author_created_at, today, cfg.account_age_days)
    ]
