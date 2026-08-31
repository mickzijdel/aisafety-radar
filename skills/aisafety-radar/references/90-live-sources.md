# Live sources

Stable endpoints for looking things up when the pack is stale or too coarse. Be polite: send a
descriptive User-Agent, pace requests, prefer feeds over pages.

## Forums (ForumMagnum GraphQL, no auth)
- LessWrong: `POST https://www.lesswrong.com/graphql`. Agent Markdown API documented at
  `https://www.lesswrong.com/api/SKILL.md` (`/api/latest`, `/api/curated`,
  `/api/search?search=`, `/api/post/<id>`, `/api/post/<id>/comments?sort=top`, `/api/tag/<slug>`).
- Alignment Forum: `POST https://www.alignmentforum.org/graphql`; same agent API.
- EA Forum: `POST https://forum-bots.effectivealtruism.org/graphql` (bot mirror; the main host
  blocks generic fetchers). Its comment views ignore `after`; filter by `postedAt` yourself.
- Posts since a date: `{ posts(input:{terms:{view:"top", after:"YYYY-MM-DD", limit:20}}) { results { title pageUrl baseScore commentCount postedAt user { displayName } } } }`
- Top comments on a post: `{ comments(input:{terms:{view:"postCommentsTop", postId:"<id>", limit:30}}) { results { baseScore user { displayName } contents { plaintextMainText } } } }`
- Wiki entry (EA Forum): `{ tags(input:{terms:{view:"tagBySlug", slug:"<slug>"}}) { results { name description { markdown } } } }`

## Newsletters and journalism (RSS)
- AI Safety Newsletter: `https://newsletter.safe.ai/feed`
- Transformer: `https://www.transformernews.ai/feed`
- Import AI: `https://importai.substack.com/feed`
- Don't Worry About the Vase: `https://thezvi.substack.com/feed`
- X-Risk Daily: `https://buttondown.com/x-risk-daily/rss`

## Data
- Epoch AI models: `https://epoch.ai/data/notable_ai_models.csv`; benchmarks
  `https://epoch.ai/data/benchmark_data.zip`
- arXiv: `https://export.arxiv.org/api/query?search_query=all:%22AI+safety%22&sortBy=submittedDate`
- 80,000 Hours career reviews: `https://80000hours.org/wp-json/wp/v2/career_profile?_fields=link,title,modified`
