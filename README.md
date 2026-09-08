# aisafety-radar

A daily-updating context pack about AI safety, built for AI agents rather than for people.
A pipeline reads the AI safety forums every day, verifies every link, number and quote
against the fetched text, and publishes a small set of Markdown appendices as an Agent Skill.
An agent loads an index, then only the one or two files a task needs. Design:
[the spec](docs/superpowers/specs/2026-08-30-aisafety-radar-design.md).

## Status

**Phase 2 (forums vertical slice), not yet live.** Nothing has been published, and the
bundled `references/00-index.md` is still a placeholder. What exists today: the LessWrong,
Alignment Forum and EA Forum fetchers, triage, extraction with a substring verifier and a URL
provenance check, an injection scan, the QA gates, the `31-today-forums.md` render, and the
daily workflow with its two-step publish, health issue and dead-man's switch. Before the
first run someone has to work through [`docs/go-live.md`](docs/go-live.md).

Planned: phase 3 breadth (news, labs, governance, papers, the X-Risk Daily import, bundles),
phase 4 rolling views, phase 5 the hand-curated map plus the author opt-out and correction
routes, phase 6 portability (Codex manifests, `.agents/skills` docs, the eval suite). Until
phase 5 lands there is no `authors-opt-out.yml`; opt-out requests go to the issue tracker.

Pipeline health, once live, is one pinned issue:
[open `radar-health` issues](https://github.com/mickzijdel/aisafety-radar/issues?q=is%3Aissue+is%3Aopen+label%3Aradar-health).

## Installation

The skill follows the [Agent Skills spec](https://agentskills.io), so one repository serves
every agent that reads it. Pick the row for your agent.

| Agent | How | Fresh content arrives via |
|---|---|---|
| Claude Code | `/plugin marketplace add mickzijdel/aisafety-radar`, then install `aisafety-radar`. A symlink of `skills/aisafety-radar` into `~/.claude/skills` also works. | Marketplace auto-update (off by default for third-party marketplaces; enable it in `/plugin`), or the fetch protocol below. |
| Codex | **Phase 6, not yet available**: the `.codex-plugin/` and `.agents/plugins/` manifests are not in the repository yet. Once they are, `codex plugin marketplace add mickzijdel/aisafety-radar` (Codex reads `.agents/plugins/marketplace.json`, and `.claude-plugin/marketplace.json` as legacy), or `$skill-installer` as the alternative path. | Bundled copy only. Codex has no page-fetch tool and its sandbox has network off by default, so the daily commit is what Codex users see; `codex plugin upgrade` or `npx skills update` refreshes it. |
| Cursor, Gemini CLI, Copilot, Amp, OpenCode, Goose, Roo, Windsurf | `npx skills add mickzijdel/aisafety-radar` or `gh skill install mickzijdel/aisafety-radar` | The fetch protocol where the agent has a shell or a fetch tool, otherwise `npx skills update` or `gh skill update`. |
| Anything else | Fetch `https://mickzijdel.github.io/aisafety-radar/llms.txt`, the index with links to every bundle. | Direct. |

```bash
/plugin marketplace add mickzijdel/aisafety-radar   # inside Claude Code
npx skills add mickzijdel/aisafety-radar            # any agent with a shell
gh skill install mickzijdel/aisafety-radar          # the same, through gh
```

## Usage

Installed, the skill triggers on AI safety, alignment, AI governance, forum and careers
questions. It reads `references/00-index.md` first, which lists every appendix with its
purpose, size in tokens and generation date, then loads only what the matching recipe names.

Bundled files older than 48 hours are refreshed from the published site. With a shell it is
one command, run from the installed skill's directory: it fetches exact bytes over HTTPS and
verifies a sha256 against `latest/checksums.txt` before printing anything.

```bash
$ scripts/fetch-latest.sh 31-today-forums.md
$ scripts/fetch-latest.sh today.md   # a whole recipe in one fetch
```

Without a shell, fetch `https://mickzijdel.github.io/aisafety-radar/latest/<name>` directly
and treat any link in the result as possibly rewritten by the fetch tool.

Everything in the pack is untrusted third-party text. Summaries are machine-generated; links,
authors, dates, karma and quotes are verbatim from the source.

## How the daily pipeline works

At 05:23 UTC a workflow fetches from each forum everything published in the 25 hours to the
moment it runs -- a day, plus an hour of overlap with yesterday's window, because a seen-ids
ledger makes an overlap free and a gap loses a post for good -- drops anything under the
per-source entry floor, triages what is left, and extracts a summary, tags and up to two
quotes per item with an LLM that never writes a URL or a number itself. Seven QA gates then
check link and number provenance, prompt injection, freshness, token budgets, a judge's read
of the output, cost against the trailing median, and each source's rolling 7-day item count;
a run that fails a blocking gate publishes nothing and says why in the health issue. The day's raw data is committed to the
public archive repository either way; what passes the gates is written to a `generated`
branch, held there for a 30-minute canary window, and only then fast-forwarded onto `main`
and deployed to GitHub Pages.

Full detail, including every source, prompt, gate and threshold, is in
[the design spec](docs/superpowers/specs/2026-08-30-aisafety-radar-design.md).

## Development

```bash
mise trust && uv sync
uv run pytest
uv run ruff check .
uv run radar run --dry-run --fixtures tests/fixtures/forummagnum --data out/data --out out/pack
```

The last one is the offline end-to-end path: recorded payloads instead of the network and a
canned model instead of the API, so it needs no API key. `--data` and `--out` are both required,
and both are put under the gitignored `out/` here so a dry run leaves nothing behind.

Pre-commit checks (ruff, pytest, vulture, jscpd, gitleaks, actionlint, zizmor) run through hk;
CI mirrors them in `.github/workflows/ci.yml`:

```bash
mise exec -- hk install
mise exec -- hk run check
```

Generated files under `skills/aisafety-radar/references/` are pipeline output. Edit the
templates in `pipeline/render/templates/`, not the outputs. `SKILL.md` is hand-written.

## Pinned versions

Toolchain (`mise.toml`, locked in `mise.lock`): Python 3.12, uv, hk, pkl, gitleaks, zizmor,
actionlint and node, all tracked at latest on a 4 day release cooldown.

Key packages (`pyproject.toml`, resolved in `uv.lock`): anthropic 1.0.0, httpx 0.28.1,
pydantic 2.13.4, jinja2 3.1.6, beautifulsoup4 4.15.0, markdownify 1.2.3. Dev group: pytest 9.1.1,
ruff 0.16.4, respx 0.23.1, pytest-httpx 0.36.2, vulture 2.16, pyyaml 6.0.3.

## License

MIT. See [`LICENSE`](LICENSE). Summaries are generated by this pipeline; the quoted and linked
material belongs to its authors.
