"""Fetchers for the three ForumMagnum forums: LessWrong, the Alignment Forum and the EA Forum."""

from __future__ import annotations

from datetime import timedelta

from pipeline.fetchers.base import Fetcher, Window
from pipeline.fetchers.forummagnum import ForumClient, RawPost
from pipeline.models import Item, Site
from pipeline.textnorm import html_to_markdown, html_to_text

SOURCE_NAMES: dict[Site, str] = {"lw": "lesswrong", "af": "alignmentforum", "eaf": "eaforum"}

# Per-source 7-day minimums: the count below which a source is broken rather than quiet. Defined
# once, because two callers build these fetchers -- `forum_fetchers`, for a caller that wants
# nothing but the fetchers, and the CLI, which has to keep the clients it built them from for the
# comment refresh. Two copies would let a threshold change reach one path and not the other.
MIN_ITEMS_7D: dict[Site, int] = {"lw": 15, "af": 3, "eaf": 5}


def raw_post_to_item(site: Site, post: RawPost) -> Item:
    html = (post.contents or {}).get("html") or ""
    user = post.user
    affiliation = None
    if user is not None:
        affiliation = user.organization or user.jobTitle
    # Only two-axis-voting sites carry an agreement score, and extendedScore is an untyped
    # blob, so anything but a number is treated as absent. `bool` is an `int` to isinstance, and
    # a vote total of True is not a vote total: it would render as "agree 1", a number the source
    # does not contain. (`pipeline/stages/comments.py` guards the comment side the same way.)
    raw_agreement = (post.extendedScore or {}).get("agreement")
    agreement = (
        int(raw_agreement)
        if isinstance(raw_agreement, int | float) and not isinstance(raw_agreement, bool)
        else None
    )
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
        ForumFetcher(site, ForumClient(site, user_agent), name, MIN_ITEMS_7D[site])
        for site, name in SOURCE_NAMES.items()
    ]
