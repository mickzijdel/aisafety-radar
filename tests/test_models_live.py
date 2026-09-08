"""Model-retirement canary: skipped without a key, run in CI where one is configured."""

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs API key")


def test_configured_models_exist():
    import anthropic

    from pipeline.config import load_settings
    from pipeline.llm import check_models

    s = load_settings()
    ok = check_models(anthropic.Anthropic(), s)
    assert set(ok) == {s.models.haiku, s.models.sonnet}
