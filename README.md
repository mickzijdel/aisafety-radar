# aisafety-radar

A daily-updating AI safety landscape skill for AI agents (Claude Code, Codex, Cursor, Gemini
CLI, Copilot and any agent that reads the Agent Skills spec). Design:
`docs/superpowers/specs/2026-08-30-aisafety-radar-design.md`.

Status: Phase 2 (forums vertical slice) under construction. Nothing is published yet.

## Development

    mise trust && uv sync
    uv run pytest
    uv run ruff check .
    uv run radar run --dry-run --fixtures tests/fixtures --out out/

Pre-commit checks (ruff, pytest, vulture, jscpd, gitleaks, actionlint, zizmor) run through hk;
CI mirrors them in `.github/workflows/ci.yml`:

    mise exec -- hk install
    mise exec -- hk run check

## Pinned versions

Toolchain (`mise.toml`, locked in `mise.lock`): Python 3.12, uv, hk, pkl, gitleaks, zizmor,
actionlint and node, all tracked at latest on a 4 day release cooldown.

Key packages (`pyproject.toml`, resolved in `uv.lock`): anthropic 1.0.0, httpx 0.28.1,
pydantic 2.13.4, jinja2 3.1.6, beautifulsoup4 4.15.0, markdownify 1.2.3, pyyaml 6.0.3,
python-dateutil 2.9.0. Dev group: pytest 9.1.1, ruff 0.16.4, respx 0.23.1, pytest-httpx 0.36.2,
vulture 2.16.

## License

MIT. See `LICENSE`.
