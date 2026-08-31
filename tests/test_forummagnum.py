import json
from datetime import UTC, date, datetime

import httpx
import respx

from pipeline.fetchers.forummagnum import SITES, ForumClient, gql_literal

UA = "aisafety-radar-test/0"

POST = {
    "_id": "abc123",
    "title": "A post",
    "pageUrl": "https://www.lesswrong.com/posts/abc123/a-post",
    "postedAt": "2026-08-29T10:00:00.000Z",
    "baseScore": 45,
    "voteCount": 20,
    "extendedScore": None,
    "commentCount": 3,
    "lastCommentedAt": "2026-08-30T01:00:00.000Z",
    "curatedDate": None,
    "frontpageDate": "2026-08-29T10:05:00.000Z",
    "user": {
        "displayName": "Alice",
        "createdAt": "2020-01-01T00:00:00.000Z",
        "karma": 900,
        "jobTitle": None,
        "organization": None,
    },
    "tags": [{"name": "AI"}],
    "contents": {"html": "<p>Body text</p>"},
}
COMMENT = {
    "_id": "c1",
    "postId": "abc123",
    "postedAt": "2026-08-30T01:00:00.000Z",
    "baseScore": 7,
    "extendedScore": {"agreement": 4},
    "parentCommentId": None,
    "topLevelCommentId": None,
    "user": {"displayName": "Bob", "createdAt": "2021-01-01T00:00:00.000Z", "karma": 100},
    "contents": {"plaintextMainText": "Nice"},
}


def test_gql_literal_unquotes_keys():
    lit = gql_literal({"view": "top", "after": "2026-08-29", "limit": 5, "flag": True, "n": None})
    assert lit == '{view: "top", after: "2026-08-29", limit: 5, flag: true, n: null}'


@respx.mock
def test_posts_since_sends_after_and_tag_filter_and_parses():
    route = respx.post(SITES["lw"].graphql_url).mock(
        return_value=httpx.Response(200, json={"data": {"posts": {"results": [POST]}}})
    )
    client = ForumClient("lw", UA, pause_seconds=0)
    posts = client.posts_since(date(2026, 8, 29))
    sent = json.loads(route.calls[0].request.content)["query"]
    assert 'after: "2026-08-29"' in sent and 'view: "top"' in sent
    assert SITES["lw"].ai_tag_id in sent and 'filterMode: "Required"' in sent
    assert route.calls[0].request.headers["user-agent"] == UA
    assert posts[0].id == "abc123" and posts[0].user.displayName == "Alice"
    assert posts[0].frontpageDate is not None


@respx.mock
def test_eaf_comments_since_filters_client_side_and_rewrites_urls():
    old = {**COMMENT, "_id": "c0", "postedAt": "2026-08-01T00:00:00.000Z"}
    respx.post(SITES["eaf"].graphql_url).mock(
        return_value=httpx.Response(200, json={"data": {"comments": {"results": [COMMENT, old]}}})
    )
    client = ForumClient("eaf", UA, pause_seconds=0)
    comments = client.comments_since(datetime(2026, 8, 29, tzinfo=UTC))
    assert [c.id for c in comments] == ["c1"]


@respx.mock
def test_eaf_post_urls_rewritten_to_public_domain():
    mirrored = {**POST, "pageUrl": "https://forum-bots.effectivealtruism.org/posts/abc123/a-post"}
    respx.post(SITES["eaf"].graphql_url).mock(
        return_value=httpx.Response(200, json={"data": {"posts": {"results": [mirrored]}}})
    )
    posts = ForumClient("eaf", UA, pause_seconds=0).posts_since(date(2026, 8, 29))
    assert posts[0].pageUrl == "https://forum.effectivealtruism.org/posts/abc123/a-post"


@respx.mock
def test_graphql_errors_raise():
    respx.post(SITES["af"].graphql_url).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad view"}]})
    )
    try:
        ForumClient("af", UA, pause_seconds=0).top_comments("x")
    except RuntimeError as e:
        assert "bad view" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
