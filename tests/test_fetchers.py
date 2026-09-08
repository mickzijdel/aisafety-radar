from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from pipeline.fetchers.base import FetchResult, Window, run_all
from pipeline.fetchers.forummagnum import RawPost
from pipeline.fetchers.forums import ForumFetcher, forum_fetchers, raw_post_to_item

RAW = RawPost.model_validate(
    {
        "_id": "abc123",
        "title": "A post",
        "pageUrl": "https://www.lesswrong.com/posts/abc123/a-post",
        "postedAt": "2026-08-29T10:00:00Z",
        "baseScore": 45,
        "voteCount": 20,
        "extendedScore": {"agreement": 12},
        "commentCount": 3,
        "curatedDate": None,
        "frontpageDate": "2026-08-29T10:05:00Z",
        "user": {
            "displayName": "Alice",
            "createdAt": "2020-01-01T00:00:00Z",
            "karma": 900,
            "organization": "METR",
        },
        "tags": [{"name": "AI"}],
        "contents": {"html": "<p>Body <b>text</b></p>"},
    }
)


def test_window_for_day():
    w = Window.for_day(date(2026, 8, 30))
    assert w.end == datetime(2026, 8, 30, 5, 40, tzinfo=UTC)
    assert (w.end - w.start).total_seconds() == 24 * 3600


def test_raw_post_to_item_maps_fields():
    item = raw_post_to_item("lw", RAW)
    assert item.id == "lw:abc123" and item.site == "lw" and item.category == "forums"
    assert item.text == "Body text" and item.markdown == "Body **text**"
    assert item.author_affiliation == "METR" and item.agreement == 12
    assert item.frontpage is True and item.curated is False
    assert item.author_created_at.year == 2020


def test_raw_post_to_item_tolerates_a_missing_user_and_body():
    bare = RawPost.model_validate(
        {
            "_id": "no-user",
            "title": "Bare",
            "pageUrl": "https://forum.effectivealtruism.org/posts/no-user/bare",
            "postedAt": "2026-08-29T10:00:00Z",
        }
    )
    item = raw_post_to_item("eaf", bare)
    assert item.author == "anonymous" and item.author_created_at is None
    assert item.author_affiliation is None and item.agreement is None
    assert item.source == "eaforum" and item.text == "" and item.markdown == ""


def test_raw_post_to_item_falls_back_to_job_title():
    post = RAW.model_copy(
        update={
            "user": RAW.user.model_copy(update={"organization": None, "jobTitle": "Researcher"}),
            "extendedScore": {},
        }
    )
    item = raw_post_to_item("af", post)
    assert item.author_affiliation == "Researcher"
    assert item.agreement is None
    assert item.source == "alignmentforum"


class FakeClient:
    def __init__(self, posts):
        self._posts = posts
        self.calls = []

    def posts_since(self, after, view="top", limit=200, require_ai_tag=True):
        self.calls.append({"after": after, "view": view, "require_ai_tag": require_ai_tag})
        return self._posts


class Boom:
    name, category, min_items_7d = "boom", "forums", 1

    def fetch(self, window):
        raise RuntimeError("network down")


def test_run_all_isolates_failures(caplog):
    good = ForumFetcher("lw", FakeClient([RAW]), "lesswrong", 15)
    with caplog.at_level(logging.ERROR, logger="pipeline.fetchers.base"):
        result = run_all([good, Boom()], Window.for_day(date(2026, 8, 30)))
    assert isinstance(result, FetchResult)
    assert [i.id for i in result.items] == ["lw:abc123"]
    by_name = {s.source: s for s in result.statuses}
    assert by_name["lesswrong"].ok and by_name["lesswrong"].items == 1
    assert not by_name["boom"].ok and "network down" in by_name["boom"].error
    # The failure has to leave a trace with a traceback, or a dead source is invisible.
    assert any(
        r.levelno == logging.ERROR and "boom" in r.getMessage() and r.exc_info
        for r in caplog.records
    )


def test_forum_fetcher_drops_posts_outside_window():
    old = RAW.model_copy(update={"postedAt": datetime(2026, 8, 1, tzinfo=UTC)})
    f = ForumFetcher("lw", FakeClient([RAW, old]), "lesswrong", 15)
    items = f.fetch(Window.for_day(date(2026, 8, 30)))
    assert [i.id for i in items] == ["lw:abc123"]


def test_forum_fetcher_asks_for_slack_before_the_window():
    client = FakeClient([])
    ForumFetcher("lw", client, "lesswrong", 15).fetch(Window.for_day(date(2026, 8, 30)))
    # Window starts 2026-08-29 05:40 UTC; a day of slack means asking from the 28th.
    assert client.calls == [{"after": date(2026, 8, 28), "view": "top", "require_ai_tag": True}]


def test_alignment_forum_does_not_require_the_ai_tag():
    # AF has no AI tag to filter on: every post there is in scope already.
    client = FakeClient([])
    ForumFetcher("af", client, "alignmentforum", 3).fetch(Window.for_day(date(2026, 8, 30)))
    assert client.calls[0]["require_ai_tag"] is False


def test_forum_fetchers_covers_the_three_sites():
    fetchers = forum_fetchers("aisafety-radar/0.1 (+mick@example.com)")
    assert [(f.name, f.site, f.min_items_7d, f.category) for f in fetchers] == [
        ("lesswrong", "lw", 15, "forums"),
        ("alignmentforum", "af", 3, "forums"),
        ("eaforum", "eaf", 5, "forums"),
    ]
    assert all(f.client.user_agent == "aisafety-radar/0.1 (+mick@example.com)" for f in fetchers)
    # A fetcher's name has to be the source its items carry, or a SourceStatus cannot be
    # joined back to the items it accounts for.
    assert all(raw_post_to_item(f.site, RAW).source == f.name for f in fetchers)


def test_agreement_of_a_boolean_is_not_a_vote_total():
    """`extendedScore` is an untyped blob, and `bool` is an `int` to isinstance.

    `{"agreement": true}` would otherwise render as `agree 1`, a number the source does not
    contain -- which the provenance and judge gates then have to explain away.
    """
    for value in (True, False):
        post = RAW.model_copy(update={"extendedScore": {"agreement": value}})
        assert raw_post_to_item("lw", post).agreement is None


def test_the_window_is_half_open_at_both_ends():
    """`window.start <= published < window.end`, pinned at both boundaries.

    The boundary is what stops a post published in the seam being carried twice, and a `<`/`<=`
    slip either way is invisible to every other test in this file.
    """
    window = Window.for_day(date(2026, 8, 30))
    at_start = RAW.model_copy(update={"id": "at-start", "postedAt": window.start})
    at_end = RAW.model_copy(update={"id": "at-end", "postedAt": window.end})
    just_before_start = RAW.model_copy(
        update={"id": "before", "postedAt": window.start - timedelta(microseconds=1)}
    )
    just_before_end = RAW.model_copy(
        update={"id": "in", "postedAt": window.end - timedelta(microseconds=1)}
    )
    posts = [at_start, at_end, just_before_start, just_before_end]
    fetcher = ForumFetcher("lw", FakeClient(posts), "lesswrong", 15)
    assert [i.id for i in fetcher.fetch(window)] == ["lw:at-start", "lw:in"]
