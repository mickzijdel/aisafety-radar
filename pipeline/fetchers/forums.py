"""Fetchers for the three ForumMagnum forums: LessWrong, the Alignment Forum and the EA Forum."""

from __future__ import annotations

from datetime import timedelta

from pipeline.fetchers.base import Fetcher, Window
from pipeline.fetchers.forummagnum import ForumClient, RawPost
from pipeline.models import Item, Site
from pipeline.textnorm import html_to_markdown, html_to_text

SOURCE_NAMES: dict[Site, str] = {"lw": "lesswrong", "af": "alignmentforum", "eaf": "eaforum"}


def raw_post_to_item(site: Site, post: RawPost) -> Item:
    html = (post.contents or {}).get("html") or ""
    user = post.user
    affiliation = None
    if user is not None:
        affiliation = user.organization or user.jobTitle
    # Only two-axis-voting sites carry an agreement score, and extendedScore is an untyped
    # blob, so anything but a number is treated as absent.
    raw_agreement = (post.extendedScore or {}).get("agreement")
    agreement = int(raw_agreement) if isinstance(raw_agreement, int | float) else None
    return Item(
        id=f"{site}:{post.id}",
        source=SOURCE_NAMES[site],
        site=site,
        category="forums",
        url=post.pageUrl,
        title=post.title,
        author=user.displayName if user else "anonymous",
        author_affiliation=affiliation,
        published_at=post.postedAt,
        score=post.baseScore,
        agreement=agreement,
        comment_count=post.commentCount,
        curated=post.curatedDate is not None,
        frontpage=post.frontpageDate is not None,
        author_created_at=user.createdAt if user else None,
        text=html_to_text(html),
        markdown=html_to_markdown(html),
    )


class ForumFetcher:
    category = "forums"

    def __init__(self, site: Site, client: ForumClient, name: str, min_items_7d: int) -> None:
        self.site = site
        self.client = client
        self.name = name
        self.min_items_7d = min_items_7d

    def fetch(self, window: Window) -> list[Item]:
        # Ask for a day of slack before the window so timezone drift never loses a post;
        # the window filter below is the real boundary.
        after = (window.start - timedelta(days=1)).date()
        # The AF is an AI-safety forum end to end and carries no AI tag to filter on.
        posts = self.client.posts_since(after, require_ai_tag=self.site != "af")
        items = [raw_post_to_item(self.site, p) for p in posts]
        return [i for i in items if window.start <= i.published_at < window.end]


def forum_fetchers(user_agent: str) -> list[Fetcher]:
    return [
        ForumFetcher("lw", ForumClient("lw", user_agent), "lesswrong", 15),
        ForumFetcher("af", ForumClient("af", user_agent), "alignmentforum", 3),
        ForumFetcher("eaf", ForumClient("eaf", user_agent), "eaforum", 5),
    ]
