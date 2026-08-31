from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

Site = Literal["lw", "af", "eaf"]


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
    postId: str
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
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"{self.cfg.site} graphql: {body['errors']}")
        return body["data"]

    def _public_url(self, url: str) -> str:
        if self.cfg.mirror_base and url.startswith(self.cfg.mirror_base):
            return self.cfg.public_base + url[len(self.cfg.mirror_base) :]
        return url

    def posts_since(
        self,
        after: date,
        view: str = "top",
        limit: int = 200,
        require_ai_tag: bool = True,
    ) -> list[RawPost]:
        terms: dict[str, Any] = {"view": view, "after": after.isoformat(), "limit": limit}
        if require_ai_tag and self.cfg.ai_tag_id:
            terms["filterSettings"] = {
                "tags": [{"tagId": self.cfg.ai_tag_id, "filterMode": "Required"}]
            }
        data = self._query(
            f"{{ posts(input: {{terms: {gql_literal(terms)}}}) {{ results {{ {POST_FIELDS} }} }} }}"
        )
        posts = [RawPost.model_validate(p) for p in data["posts"]["results"]]
        for p in posts:
            p.pageUrl = self._public_url(p.pageUrl)
        return posts

    def comments_since(self, after: datetime, limit: int = 5000) -> list[RawComment]:
        terms: dict[str, Any] = {"view": "recentComments", "limit": limit}
        if self.cfg.comments_after_supported:
            terms["after"] = after.astimezone(UTC).isoformat().replace("+00:00", "Z")
        else:
            terms["limit"] = min(limit, 1000)
        data = self._query(
            f"{{ comments(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ {COMMENT_FIELDS} }} }} }}"
        )
        comments = [RawComment.model_validate(c) for c in data["comments"]["results"]]
        return [c for c in comments if c.postedAt >= after]

    def top_comments(self, post_id: str, limit: int = 30) -> list[RawComment]:
        terms = {"view": "postCommentsTop", "postId": post_id, "limit": limit}
        data = self._query(
            f"{{ comments(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ {COMMENT_FIELDS} }} }} }}"
        )
        return [RawComment.model_validate(c) for c in data["comments"]["results"]]

    def tag_by_slug(self, slug: str) -> dict[str, Any] | None:
        terms = {"view": "tagBySlug", "slug": slug}
        data = self._query(
            f"{{ tags(input: {{terms: {gql_literal(terms)}}}) "
            f"{{ results {{ name slug postCount description {{ markdown }} }} }} }}"
        )
        results = data["tags"]["results"]
        return results[0] if results else None
