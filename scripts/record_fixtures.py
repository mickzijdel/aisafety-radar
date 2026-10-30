"""Record live ForumMagnum responses into tests/fixtures/forummagnum/ for realistic tests.

The files hold the RAW GraphQL `results` payloads exactly as the forums returned them — no
model validation, no client-side filtering — so the fixture test validating them against
RawPost/RawComment catches a parsing regression against real data.

Run by hand, never in CI: it makes live requests to lesswrong.com, alignmentforum.org and the
EA Forum bot mirror, paced at one request per second by the client.

Usage: uv run python scripts/record_fixtures.py YYYY-MM-DD [site ...]

  uv run python scripts/record_fixtures.py 2026-08-30        # all three sites
  uv run python scripts/record_fixtures.py 2026-08-10 af     # re-record one site, wider window
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from pipeline.config import user_agent
from pipeline.fetchers.forummagnum import SITES, ForumClient


def usage(problem: str) -> NoReturn:
    print(f"error: {problem}", file=sys.stderr)
    print("usage: uv run python scripts/record_fixtures.py YYYY-MM-DD [site ...]", file=sys.stderr)
    print(f"       sites: {' '.join(SITES)} (default: all)", file=sys.stderr)
    raise SystemExit(2)


UA = user_agent("dev")

if len(sys.argv) < 2:
    usage("missing the YYYY-MM-DD date to record from")
try:
    since = datetime.fromisoformat(sys.argv[1])
except ValueError as exc:
    usage(f"{sys.argv[1]!r} is not an ISO date: {exc}")
sites = sys.argv[2:] or list(SITES)
for site in sites:
    if site not in SITES:
        usage(f"unknown site {site!r}")

out = Path("tests/fixtures/forummagnum")
out.mkdir(parents=True, exist_ok=True)


def dump(name: str, records: list[dict[str, Any]]) -> None:
    (out / name).write_text(json.dumps(records, indent=1) + "\n")


for site in sites:
    # Drop this site's previous recording first, so an empty result cannot leave a stale file
    # behind (a post with no comments, for instance, writes no top-comments file at all).
    for stale in out.glob(f"{site}_*.json"):
        stale.unlink()
    c = ForumClient(site, UA)
    posts = c.posts_since_raw(since.date(), limit=20)
    dump(f"{site}_posts.json", posts)
    comments = c.comments_since_raw(since.replace(tzinfo=UTC), limit=200)
    dump(f"{site}_comments.json", comments)
    top: list[dict[str, Any]] = []
    if posts:
        top = c.top_comments_raw(posts[0]["_id"])
        dump(f"{site}_top_comments.json", top)
    print(site, len(posts), "posts", len(comments), "comments", len(top), "top comments")
