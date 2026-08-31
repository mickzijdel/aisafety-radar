import pytest
from pydantic import ValidationError

from pipeline.config import Settings, load_settings, user_agent


def test_settings_load_defaults():
    s = load_settings()
    assert s.models.haiku == "claude-haiku-4-5" and s.models.sonnet == "claude-sonnet-5"
    assert s.prices["claude-haiku-4-5"].output == 5.0
    assert s.caps.forums_per_day == 25
    assert s.floor.post_karma_min["lw"] == 20
    assert s.budgets["31-today-forums"] == 9000


def test_user_agent():
    ua = user_agent("0.1.0")
    assert ua.startswith("aisafety-radar/0.1.0 (") and "contact:" in ua


def test_every_configured_model_has_a_price():
    """A model id with no price entry would make cost accounting raise mid-run."""
    s = load_settings()
    assert {s.models.haiku, s.models.sonnet} <= set(s.prices)


def test_load_settings_reads_the_given_path(tmp_path):
    config = tmp_path / "other.toml"
    config.write_text(
        '[models]\nhaiku = "h"\nsonnet = "s"\n[prices.h]\ninput = 1.0\noutput = 2.0\n'
        "cache_read = 0.5\n"
    )
    s = load_settings(config)
    assert s.models.haiku == "h"
    # Everything the file leaves out falls back to the model defaults.
    assert s.caps.forums_per_day == 25 and s.floor.per_author_per_day == 3


def test_unknown_key_is_rejected(tmp_path):
    """A typo in config.toml must fail loudly rather than be silently ignored."""
    config = tmp_path / "typo.toml"
    config.write_text(
        '[models]\nhaiku = "h"\nsonnet = "s"\n[prices.h]\ninput = 1.0\noutput = 2.0\n'
        "cache_read = 0.5\n[caps]\nforums_per_dya = 25\n"
    )
    with pytest.raises(ValidationError):
        load_settings(config)


def test_settings_requires_models():
    with pytest.raises(ValidationError):
        Settings.model_validate({"prices": {}})
