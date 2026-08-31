import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from pipeline.fetchers.forummagnum import (
    SITES,
    ForumClient,
    RawComment,
    RawPost,
    gql_literal,
)

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


# --- the queries we actually send -------------------------------------------------------------
# Every view name below is a literal copied from a verified live request: ForumMagnum silently
# falls back to a default view when it does not recognise one, so a typo would fail silently.


def sent_query(route, index: int = 0) -> str:
    return json.loads(route.calls[index].request.content)["query"]


def mock_site(site: str, field: str, results: list[dict]):
    return respx.post(SITES[site].graphql_url).mock(
        return_value=httpx.Response(200, json={"data": {field: {"results": results}}})
    )


@respx.mock
def test_eaf_comments_since_omits_after_and_caps_limit_at_1000():
    route = mock_site("eaf", "comments", [])
    ForumClient("eaf", UA, pause_seconds=0).comments_since(
        datetime(2026, 8, 29, tzinfo=UTC), limit=5000
    )
    query = sent_query(route)
    assert 'view: "recentComments"' in query
    assert "after:" not in query  # EAF silently ignores it, so we never send it
    assert "limit: 1000" in query


@respx.mock
def test_lw_comments_since_sends_after_and_the_full_limit():
    route = mock_site("lw", "comments", [])
    ForumClient("lw", UA, pause_seconds=0).comments_since(
        datetime(2026, 8, 29, tzinfo=UTC), limit=5000
    )
    query = sent_query(route)
    assert 'view: "recentComments"' in query
    assert 'after: "2026-08-29T00:00:00Z"' in query
    assert "limit: 5000" in query


@respx.mock
def test_comments_since_treats_a_naive_after_as_utc():
    route = mock_site("lw", "comments", [COMMENT])
    comments = ForumClient("lw", UA, pause_seconds=0).comments_since(datetime(2026, 8, 29))
    assert 'after: "2026-08-29T00:00:00Z"' in sent_query(route)
    assert [c.id for c in comments] == ["c1"]  # and the comparison does not blow up


@respx.mock
def test_top_comments_sends_post_comments_top_view_and_post_id():
    route = mock_site("lw", "comments", [COMMENT])
    comments = ForumClient("lw", UA, pause_seconds=0).top_comments("abc123", limit=7)
    query = sent_query(route)
    assert 'view: "postCommentsTop"' in query
    assert 'postId: "abc123"' in query
    assert "limit: 7" in query
    assert [c.id for c in comments] == ["c1"]


@respx.mock
def test_tag_by_slug_sends_tag_by_slug_view_and_parses_the_first_result():
    tag = {"name": "AI", "slug": "ai", "postCount": 20143, "description": {"markdown": "…"}}
    route = mock_site("lw", "tags", [tag])
    result = ForumClient("lw", UA, pause_seconds=0).tag_by_slug("ai")
    query = sent_query(route)
    assert 'view: "tagBySlug"' in query and 'slug: "ai"' in query
    assert result == tag


@respx.mock
def test_tag_by_slug_returns_none_when_there_are_no_results():
    mock_site("af", "tags", [])
    assert ForumClient("af", UA, pause_seconds=0).tag_by_slug("no-such-tag") is None


@respx.mock
def test_af_posts_since_sends_no_tag_filter():
    route = mock_site("af", "posts", [])
    ForumClient("af", UA, pause_seconds=0).posts_since(date(2026, 8, 29))
    query = sent_query(route)
    assert "filterSettings" not in query  # AF has no AI tag: the whole forum is in scope
    assert 'after: "2026-08-29"' in query


@respx.mock
def test_posts_since_can_skip_the_tag_filter_on_request():
    route = mock_site("lw", "posts", [])
    ForumClient("lw", UA, pause_seconds=0).posts_since(date(2026, 8, 29), require_ai_tag=False)
    assert "filterSettings" not in sent_query(route)


# --- tolerance for records the schema rejects --------------------------------------------------


@respx.mock
def test_one_malformed_post_does_not_lose_the_batch(caplog):
    mock_site("lw", "posts", [POST, {"_id": "broken"}])
    with caplog.at_level(logging.WARNING):
        posts = ForumClient("lw", UA, pause_seconds=0).posts_since(date(2026, 8, 29))
    assert [p.id for p in posts] == ["abc123"]
    assert "broken" in caplog.text


@respx.mock
def test_one_malformed_comment_does_not_lose_the_batch(caplog):
    mock_site("lw", "comments", [COMMENT, {"_id": "broken", "postId": "abc123"}])
    with caplog.at_level(logging.WARNING):
        comments = ForumClient("lw", UA, pause_seconds=0).comments_since(
            datetime(2026, 8, 29, tzinfo=UTC)
        )
    assert [c.id for c in comments] == ["c1"]
    assert "broken" in caplog.text


@respx.mock
def test_comments_without_a_post_id_are_dropped():
    orphan = {**COMMENT, "_id": "c-orphan", "postId": None}
    mock_site("lw", "comments", [COMMENT, orphan])
    client = ForumClient("lw", UA, pause_seconds=0)
    assert [c.id for c in client.comments_since(datetime(2026, 8, 29, tzinfo=UTC))] == ["c1"]
    mock_site("lw", "comments", [COMMENT, orphan])
    assert [c.id for c in client.top_comments("abc123")] == ["c1"]


# --- transport and response-shape failures -----------------------------------------------------


@respx.mock
def test_http_error_with_a_graphql_body_raises_the_site_named_error():
    respx.post(SITES["eaf"].graphql_url).mock(
        return_value=httpx.Response(400, json={"errors": [{"message": "unknown field jobTitle"}]})
    )
    with pytest.raises(RuntimeError, match="eaf graphql.*unknown field jobTitle"):
        ForumClient("eaf", UA, pause_seconds=0).posts_since(date(2026, 8, 29))


@respx.mock
def test_http_error_without_a_graphql_body_still_raises_for_status():
    respx.post(SITES["lw"].graphql_url).mock(
        return_value=httpx.Response(429, text="Vercel Security Checkpoint")
    )
    with pytest.raises(httpx.HTTPStatusError):
        ForumClient("lw", UA, pause_seconds=0).posts_since(date(2026, 8, 29))


@respx.mock
def test_null_result_node_raises_instead_of_typeerror():
    respx.post(SITES["lw"].graphql_url).mock(
        return_value=httpx.Response(200, json={"data": {"posts": None}})
    )
    with pytest.raises(RuntimeError, match="no posts results"):
        ForumClient("lw", UA, pause_seconds=0).posts_since(date(2026, 8, 29))


@respx.mock
def test_null_results_list_raises_instead_of_typeerror():
    respx.post(SITES["lw"].graphql_url).mock(
        return_value=httpx.Response(200, json={"data": {"comments": {"results": None}}})
    )
    with pytest.raises(RuntimeError, match="no comments results"):
        ForumClient("lw", UA, pause_seconds=0).comments_since(datetime(2026, 8, 29, tzinfo=UTC))


# --- the recorded fixtures ---------------------------------------------------------------------
# These are RAW server payloads (scripts/record_fixtures.py), so validating every record against
# the models turns a real forum response into regression bait for the parsing layer.

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "forummagnum"
FIXTURE_KINDS = {"posts": RawPost, "comments": RawComment, "top_comments": RawComment}


def fixture_names() -> list[str]:
    expected = {f"{site}_{kind}.json" for site in SITES for kind in FIXTURE_KINDS}
    present = {p.name for p in FIXTURE_DIR.glob("*.json")} if FIXTURE_DIR.is_dir() else set()
    return sorted(expected | present)


def load_fixture(name: str) -> list[dict]:
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"{name} has not been recorded")
    return json.loads(path.read_text())


@pytest.mark.parametrize("name", fixture_names())
def test_every_recorded_record_validates(name):
    records = load_fixture(name)
    kind = name.split("_", 1)[1].removesuffix(".json")  # "lw_top_comments.json" -> "top_comments"
    model = FIXTURE_KINDS.get(kind)
    assert model is not None, f"unexpected fixture {name}"
    assert isinstance(records, list)
    for record in records:
        model.model_validate(record)


def test_recorded_eaf_posts_are_raw_mirror_payloads():
    records = load_fixture("eaf_posts.json")
    if not records:
        pytest.skip("no EAF posts recorded for that day")
    # Raw means un-rewritten: the mirror domain survives in the fixture and only the client
    # turns it into a public URL.
    assert records[0]["pageUrl"].startswith(SITES["eaf"].mirror_base)
    assert RawPost.model_validate(records[0]).pageUrl.startswith(SITES["eaf"].mirror_base)
