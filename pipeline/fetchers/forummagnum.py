from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipeline.models import Site

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteConfig:
    site: Site
    graphql_url: str
    public_base: str
    ai_tag_id: str | None
    comments_after_supported: bool
    mirror_base: str | None = None


SITES: dict[Site, SiteConfig] = {
    "lw": SiteConfig(
        "lw",
        "https://www.lesswrong.com/graphql",
        "https://www.lesswrong.com",
        "sYm3HiWcfZvrGu3ui",
        True,
    ),
    "af": SiteConfig(
        "af",
        "https://www.alignmentforum.org/graphql",
        "https://www.alignmentforum.org",
        None,
        True,
    ),
    "eaf": SiteConfig(
        "eaf",
        "https://forum-bots.effectivealtruism.org/graphql",
        "https://forum.effectivealtruism.org",
        "oNiQsBHA3i837sySD",
        False,
        mirror_base="https://forum-bots.effectivealtruism.org",
    ),
}

# If the probe (Task 19) shows a site rejecting jobTitle/organization, remove them here.
USER_FIELDS = "user { displayName createdAt karma jobTitle organization }"
POST_FIELDS = (
    "_id title pageUrl postedAt baseScore voteCount extendedScore commentCount "
    "lastCommentedAt curatedDate frontpageDate " + USER_FIELDS + " tags { name } contents { html }"
)
COMMENT_FIELDS = (
    "_id postId postedAt baseScore extendedScore parentCommentId topLevelCommentId "
    + USER_FIELDS
    + " contents { plaintextMainText }"
)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class RawUser(_Loose):
    displayName: str = "anonymous"
    createdAt: datetime | None = None
    karma: int | None = None
    jobTitle: str | None = None
    organization: str | None = None


class RawPost(_Loose):
    id: str = Field(alias="_id")
    title: str
    pageUrl: str
    postedAt: datetime
    baseScore: int | None = None
    voteCount: int | None = None
    extendedScore: dict[str, Any] | None = None
    commentCount: int = 0
    lastCommentedAt: datetime | None = None
    curatedDate: datetime | None = None
    frontpageDate: datetime | None = None
    user: RawUser | None = None
    tags: list[dict[str, Any]] = Field(default_factory=list)
    contents: dict[str, Any] | None = None


class RawComment(_Loose):
    id: str = Field(alias="_id")
    # Deleted comments and some mirror records arrive without a parent post; those are dropped
    # by the client rather than failing the whole batch.
    postId: str | None = None
    postedAt: datetime
    baseScore: int | None = None
    extendedScore: dict[str, Any] | None = None
    parentCommentId: str | None = None
    topLevelCommentId: str | None = None
    user: RawUser | None = None
    contents: dict[str, Any] | None = None


def gql_literal(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {gql_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(gql_literal(v) for v in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value)


class ForumClient:
    def __init__(
        self,
        site: Site,
        user_agent: str,
        http: httpx.Client | None = None,
        pause_seconds: float = 1.0,
    ) -> None:
        self.cfg = SITES[site]
        self.http = http or httpx.Client(timeout=60)
        self.user_agent = user_agent
        self.pause_seconds = pause_seconds
        self._last_call = 0.0

    def _query(self, query: str) -> dict[str, Any]:
        wait = self.pause_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = self.http.post(
            self.cfg.graphql_url,
            json={"query": query},
            headers={"User-Agent": self.user_agent, "Content-Type": "application/json"},
        )
        self._last_call = time.monotonic()
        if resp.is_error:
            # A GraphQL error body says far more than the bare status line, so surface it when
            # the failing response carries one instead of letting raise_for_status hide it.
            try:
                failed = resp.json()
            except ValueError:
                failed = None
            if isinstance(failed, dict) and failed.get("errors"):
                raise RuntimeError(f"{self.cfg.site} graphql: {failed['errors']}")
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"{self.cfg.site} graphql: {body['errors']}")
        data = body.get("data")
        if data is None:
            raise RuntimeError(f"{self.cfg.site} graphql: response carried no data")
        return data

    def _results(self, field: str, query: str) -> list[dict[str, Any]]:
        """Run `query` and return `data[field].results`, refusing a null node or result list."""
        data = self._query(query)
        node = data.get(field)
        if not isinstance(node, dict) or node.get("results") is None:
            raise RuntimeError(f"{self.cfg.site} graphql: no {field} results in response")
        return node["results"]

    def _parse_many(self, model: type[_Loose], records: list[dict[str, Any]]) -> list[Any]:
        """Validate a batch, skipping (and logging) the records the schema rejects."""
        parsed = []
        for record in records:
            try:
                parsed.append(model.model_validate(record))
            except ValidationError as exc:
                log.warning(
                    "%s: skipping unparseable %s record %r: %s",
                    self.cfg.site,
                    model.__name__,
                    (record or {}).get("_id") if isinstance(record, dict) else None,
                    exc,
                )
        return parsed

    def _public_url(self, url: str) -> str:
        if self.cfg.mirror_base and url.startswith(self.cfg.mirror_base):
            return self.cfg.public_base + url[len(self.cfg.mirror_base) :]
        return url

    def posts_since_raw(
        self,
        after: date,
        view: str = "top",
        limit: int = 200,
        require_ai_tag: bool = True,
    ) -> list[dict[str, Any]]:
        """The server's own post records, untouched — what the fixture recorder stores."""
        terms: dict[str, Any] = {"view": view, "after": after.isoformat(), "limit": limit}
        if require_ai_tag and self.cfg.ai_tag_id:
            terms["filterSettings"] = {
                "tags": [{"tagId": self.cfg.ai_tag_id, "filterMode": "Required"}]
            }
        return self._results(
            "posts",
            f"{{ posts(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ {POST_FIELDS} }} }} }}",
        )

    def posts_since(
        self,
        after: date,
        view: str = "top",
        limit: int = 200,
        require_ai_tag: bool = True,
    ) -> list[RawPost]:
        raw = self.posts_since_raw(after, view=view, limit=limit, require_ai_tag=require_ai_tag)
        posts: list[RawPost] = self._parse_many(RawPost, raw)
        for p in posts:
            p.pageUrl = self._public_url(p.pageUrl)
        return posts

    def comments_since_raw(self, after: datetime, limit: int = 5000) -> list[dict[str, Any]]:
        """The server's own comment records, unfiltered — what the fixture recorder stores."""
        terms: dict[str, Any] = {"view": "recentComments", "limit": limit}
        if self.cfg.comments_after_supported:
            terms["after"] = after.astimezone(UTC).isoformat().replace("+00:00", "Z")
        else:
            # This site ignores `after` on comment views, so pull a capped window and filter
            # client-side rather than asking for a bound it will silently drop.
            terms["limit"] = min(limit, 1000)
        return self._results(
            "comments",
            f"{{ comments(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ {COMMENT_FIELDS} }} }} }}",
        )

    def comments_since(self, after: datetime, limit: int = 5000) -> list[RawComment]:
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        raw = self.comments_since_raw(after, limit=limit)
        comments: list[RawComment] = self._parse_many(RawComment, raw)
        return [c for c in comments if c.postId is not None and c.postedAt >= after]

    def top_comments_raw(self, post_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """The server's own comment records for one post — what the fixture recorder stores."""
        terms = {"view": "postCommentsTop", "postId": post_id, "limit": limit}
        return self._results(
            "comments",
            f"{{ comments(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ {COMMENT_FIELDS} }} }} }}",
        )

    def top_comments(self, post_id: str, limit: int = 30) -> list[RawComment]:
        comments: list[RawComment] = self._parse_many(
            RawComment, self.top_comments_raw(post_id, limit=limit)
        )
        return [c for c in comments if c.postId is not None]

    def tag_by_slug(self, slug: str) -> dict[str, Any] | None:
        terms = {"view": "tagBySlug", "slug": slug}
        results = self._results(
            "tags",
            f"{{ tags(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ name slug postCount description {{ markdown }} }} }} }}",
        )
        return results[0] if results else None
