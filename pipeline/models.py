"""Shared data models for the pipeline: items, comments, extractions, gates and usage."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args

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
CLAIM_TYPES: frozenset[str] = frozenset(get_args(ClaimType))

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


# Repairs for the fields whose bounds `_strict_schema` has to strip before the API will accept the
# schema (see the bottom of this module). The model is told the bounds in the prompt and mostly
# honours them, but "mostly" is not a guarantee, and nothing here may raise: a whole item dropping
# out of the digest is a worse failure than one wrong slug. Model output is arbitrary JSON, so a
# field can arrive as null, a number, a list or a dict, and every one of those has to land
# somewhere valid.


def coerce_choice(value: Any, allowed: frozenset[str], fallback: str) -> str:
    """`value` if it is one of `allowed`, else `fallback`."""
    return value if isinstance(value, str) and value in allowed else fallback


def coerce_slugs(value: Any, limit: int, fallback: tuple[str, ...] = ()) -> list[str]:
    """The known slugs in `value`, capped at `limit`; `fallback` if none survive."""
    known = (
        [s for s in value if isinstance(s, str) and s in SLUGS] if isinstance(value, list) else []
    )
    return known[:limit] or list(fallback)


def coerce_importance(value: Any) -> int:
    """`value` as an int clamped to 1-5, or 1 if it is not a number at all.

    `OverflowError` is in the tuple because `json.loads` accepts the bare literal `Infinity`,
    and `int(float("inf"))` raises it rather than the ValueError `NaN` raises. Uncaught, that
    takes the item out of the digest over a field with a safe default.
    """
    try:
        return min(5, max(1, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 1


def coerce_str_list(value: Any, limit: int) -> list[str]:
    """The strings in `value`, capped at `limit`; empty if `value` is not a list."""
    return [s for s in value if isinstance(s, str)][:limit] if isinstance(value, list) else []


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
    # Writing the system prompt into the cache is billed separately, at a premium over the input
    # rate. Left out, the run's cost reads low against the Console bill the README invites the
    # reader to check it against.
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


# Validation keywords the API's json_schema output format does not accept. Passing one through
# risks a 400, or a constraint that silently does nothing. `_strict_schema` strips them; the
# pydantic Field constraints that produce them stay, so model_validate still enforces them on the
# parsed response.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "uniqueItems",
    }
)


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Set additionalProperties: false on every object and require every property.

    The API's json_schema format needs both; pydantic emits neither for optional fields.
    Keywords the format rejects are dropped from every node on the way through.
    """
    for keyword in _UNSUPPORTED_KEYWORDS & schema.keys():
        del schema[keyword]
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
