"""Runtime settings, loaded from `pipeline/config.toml`.

`config.toml` is the source of truth: the defaults on the models below only cover a section the
file leaves out entirely. Every model forbids unknown keys, so a typo in the file is a load-time
error rather than a silently ignored setting.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import Field

from pipeline.floor import FloorConfig
from pipeline.models import Strict


class ModelIds(Strict):
    haiku: str
    sonnet: str


class Price(Strict):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float


class Caps(Strict):
    forums_per_day: int = 25


class Thresholds(Strict):
    mover_new_comments: int = 10
    mover_score_change: float = 0.25
    cost_alert_multiplier: float = 2.0
    judge_sample_rate: float = 0.05


class Paths(Strict):
    skill_dir: str = "skills/aisafety-radar"


class Settings(Strict):
    models: ModelIds
    prices: dict[str, Price]
    caps: Caps = Field(default_factory=Caps)
    budgets: dict[str, int] = Field(default_factory=dict)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    floor: FloorConfig = Field(default_factory=FloorConfig)
    paths: Paths = Field(default_factory=Paths)


DEFAULT_CONFIG = Path(__file__).with_name("config.toml")


def load_settings(path: Path | None = None) -> Settings:
    with open(path or DEFAULT_CONFIG, "rb") as f:
        return Settings.model_validate(tomllib.load(f))


def user_agent(version: str) -> str:
    """Identify the pipeline to every host it fetches from, with a way to reach its owner."""
    return (
        f"aisafety-radar/{version} "
        "(+https://github.com/mickzijdel/aisafety-radar; contact: claude@mickzijdel.com)"
    )
