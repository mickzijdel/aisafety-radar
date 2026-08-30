# aisafety-radar

Read `docs/superpowers/specs/2026-08-30-aisafety-radar-design.md` before changing the pipeline.
Current plan checkpoint: `.claude/current_plan.md`.

- Python 3.12 via uv: `uv run pytest`, `uv run ruff check .`. Never `pip install`.
- Model IDs only in `pipeline/config.toml`. No Batch API. Haiku has no `thinking` parameter.
- The model never writes a URL or a metadata line; see spec 6.3 and `pipeline/stages/verify.py`.
- Generated files under `skills/aisafety-radar/references/` are written by the pipeline; edit
  templates in `pipeline/render/templates/`, not the outputs. `SKILL.md` is hand-written.
- Prose: no em dashes.

## Toolchain and pinned versions

Tools come from `mise.toml` (locked in `mise.lock`): Python 3.12, uv, hk, pkl, gitleaks, zizmor,
actionlint, node. Read versions from `uv.lock` and `mise.lock`, never from memory.

- Test: `uv run pytest`. Lint: `uv run ruff check .` and `uv run ruff format --check .`.
- Full pre-commit suite: `mise exec -- hk run check` (ruff, pytest, vulture, jscpd, gitleaks,
  actionlint, zizmor, large files, exec bits, version sync). CI mirrors it in
  `.github/workflows/ci.yml`.
- Key packages: anthropic 1.0.0, httpx 0.28.1, pydantic 2.13.4, jinja2 3.1.6,
  beautifulsoup4 4.15.0, markdownify 1.2.3, pyyaml 6.0.3, python-dateutil 2.9.0. Dev group:
  pytest 9.1.1, ruff 0.16.4, respx 0.23.1, pytest-httpx 0.36.2, vulture 2.16.
- Dependency resolution runs on a 4 day cooldown (`[tool.uv] exclude-newer`).
