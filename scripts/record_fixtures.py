"""Record live ForumMagnum responses into tests/fixtures/forummagnum/ for realistic tests.

Usage: uv run python scripts/record_fixtures.py 2026-08-29
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pipeline.fetchers.forummagnum import ForumClient

UA = (
    "aisafety-radar/dev (+https://github.com/mickzijdel/aisafety-radar; "
    "contact: claude@mickzijdel.com)"
)
out = Path("tests/fixtures/forummagnum")
out.mkdir(parents=True, exist_ok=True)
after = sys.argv[1]
for site in ("lw", "af", "eaf"):
    c = ForumClient(site, UA)
    posts = c.posts_since(datetime.fromisoformat(after).date(), limit=20)
    (out / f"{site}_posts.json").write_text(
        json.dumps([p.model_dump(by_alias=True, mode="json") for p in posts], indent=1)
    )
    comments = c.comments_since(datetime.fromisoformat(after).replace(tzinfo=UTC), limit=200)
    (out / f"{site}_comments.json").write_text(
        json.dumps([x.model_dump(by_alias=True, mode="json") for x in comments], indent=1)
    )
    if posts:
        top = c.top_comments(posts[0].id)
        (out / f"{site}_top_comments.json").write_text(
            json.dumps([x.model_dump(by_alias=True, mode="json") for x in top], indent=1)
        )
    print(site, len(posts), "posts", len(comments), "comments")
