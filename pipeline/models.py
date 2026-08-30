"""Shared data models for the pipeline: items, comments, extractions, gates and usage."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Site = Literal["lw", "af", "eaf"]
Category = Literal["forums", "news", "governance", "papers", "events", "imports"]
ClaimType = Literal[
    "confident-empirical",
    "confident-argument",
    "exploratory",
    "speculation",
    "question",
    "announcement",
]

SLUGS: frozenset[str] = frozenset(
    {
        # technical
        "interp",
        "evals",
        "control",
        "oversight",
        "training",
        "deception",
        "cot-monitoring",
        "agent-security",
        "unlearning",
        "ai-for-safety",
        "agent-foundations",
        "robustness",
        "multi-agent",
        "welfare",
        # governance
        "compute-gov",
        "lab-policy",
        "regulation",
        "international",
        "gov-evals",
        "standards",
        "open-weights",
        "military",
        # strategy
        "timelines",
        "scenarios",
        "threat-models",
        "macrostrategy",
        "trends",
        "forecasts",
        "advocacy",
        # risk context
        "releases",
        "agents",
        "incidents",
        "adjacent-xrisk",
        # ecosystem
        "orgs",
        "funding",
        "programmes",
        "community",
        "culture",
        "events",
        # careers
        "careers",
    }
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Item(Strict):
    id: str
    source: str
    site: Site | None
    category: Category
    url: str
    title: str
    author: str
    author_affiliation: str | None
    published_at: datetime
    score: int | None
    agreement: int | None
    comment_count: int
    curated: bool
    frontpage: bool
    author_created_at: datetime | None
    text: str
    markdown: str


class Comment(Strict):
    id: str
    post_id: str
    site: Site
    author: str
    author_created_at: datetime | None
    posted_at: datetime
    score: int
    agreement: int | None
    parent_id: str | None
    text: str


class SourceStatus(Strict):
    source: str
    category: Category
    ok: bool
    items: int
    error: str | None = None


class TriageResult(Strict):
    item_id: str
    relevant: bool
    primary_slug: str
    secondary_slugs: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=5)


class Extraction(Strict):
    item_id: str
    summary: str
    key_quotes: list[str] = Field(default_factory=list, max_length=2)
    author_stated_status: str | None
    claim_type: ClaimType
    novelty: str | None
    discussion_signal: str | None
    deadline: str | None
    entities: list[str] = Field(default_factory=list)
    slugs: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=5)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return _strict_schema(cls.model_json_schema())


class GateResult(Strict):
    name: str
    passed: bool
    blocking: bool
    details: list[str] = Field(default_factory=list)


class Usage(Strict):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Set additionalProperties: false on every object and require every property.

    The API's json_schema format needs both; pydantic emits neither for optional fields.
    """
    if schema.get("type") == "object" and "properties" in schema:
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"].keys())
        for prop in schema["properties"].values():
            _strict_schema(prop)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []):
            _strict_schema(sub)
    if "items" in schema and isinstance(schema["items"], dict):
        _strict_schema(schema["items"])
    for sub in schema.get("$defs", {}).values():
        _strict_schema(sub)
    return schema
