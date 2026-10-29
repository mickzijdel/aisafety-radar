"""A `ForumClient` stand-in that replays the recorded ForumMagnum responses.

`scripts/record_fixtures.py` stores the servers' own JSON, untouched, so this client has the same
job the real one does: validate each record into a `RawPost` or `RawComment` and rewrite the EA
Forum's mirror URLs to the public domain. It borrows `ForumClient._public_url` rather than
restating the rewrite, so a change to a site's mirror cannot leave the two disagreeing; that method
reads nothing but `self.cfg`, which is why binding it to this class works.

The recorded payloads were fetched with the same terms the real client sends (`view: top`, the AI
tag required where the site has one), so `view` and `require_ai_tag` are accepted and ignored here:
what they would select is already baked into the file. `after` and `limit` are applied, because the
run asks for two different windows out of the same recording. So is `fields`: it is the one term
that changes the *shape* of what comes back rather than which records do, and replaying bodies to
a caller that asked for none would make the offline run a more generous test than the live one.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.fetchers.base import Window
from pipeline.fetchers.forummagnum import (
    POST_FIELDS,
    SITES,
    ForumClient,
    RawComment,
    RawPost,
)
from pipeline.models import Site


class FixtureClient:
    """Replays recorded ForumMagnum responses from a fixtures directory."""

    def __init__(self, site: Site, root: Path) -> None:
        self.site = site
        self.root = Path(root)
        self.cfg = SITES[site]

    # Reuse, not a copy: `_public_url` only ever reads `self.cfg`.
    _public_url = ForumClient._public_url

    def _load(self, name: str, model: type[Any]) -> list[Any]:
        path = self.root / f"{self.site}_{name}.json"
        if not path.exists():
            return []
        return [model.model_validate(record) for record in json.loads(path.read_text())]

    def posts_since(
        self,
        after: date,
        view: str = "top",
        limit: int = 200,
        require_ai_tag: bool = True,
        fields: str = POST_FIELDS,
    ) -> list[RawPost]:
        posts: list[RawPost] = self._load("posts", RawPost)
        kept = [p for p in posts if p.postedAt.date() >= after][:limit]
        for p in kept:
            p.pageUrl = self._public_url(p.pageUrl)
            # `fields` is the one argument that changes what comes *back*, so unlike `view` and
            # `require_ai_tag` it cannot be accepted and ignored: a caller asking for the lean set
            # gets records with no body, the way the server would send them. Replaying full
            # records to the window fetch would hide exactly what the lean set changes -- how much
            # source text reaches the provenance gate's allowed set.
            if "contents" not in fields:
                p.contents = None
        return kept

    def comments_since(self, after: datetime, limit: int = 5000) -> list[RawComment]:
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        comments: list[RawComment] = self._load("comments", RawComment)
        return [c for c in comments if c.postId is not None and c.postedAt >= after][:limit]

    def top_comments(self, post_id: str, limit: int = 30) -> list[RawComment]:
        comments: list[RawComment] = self._load("top_comments", RawComment)
        return [c for c in comments if c.postId == post_id][:limit]

    @staticmethod
    def day_of(root: Path) -> date:
        """The day whose 24-hour window holds the newest recorded post.

        A run's window ends at 05:40 UTC on its own day, so a post recorded before that instant is
        already inside its own day's window and only a later one belongs to the next day's. An
        unconditional "the day after the newest post" would put an early-morning recording *before*
        the window start and render nothing at all, which is why the window itself is consulted.
        """
        newest: datetime | None = None
        for path in Path(root).glob("*_posts.json"):
            for post in json.loads(path.read_text()):
                moment = datetime.fromisoformat(post["postedAt"])
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                newest = moment if newest is None or moment > newest else newest
        assert newest is not None, "no fixtures recorded; run scripts/record_fixtures.py"
        day = newest.date()
        return day if newest < Window.for_day(day).end else day + timedelta(days=1)
