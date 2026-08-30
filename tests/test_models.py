from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pipeline.models import SLUGS, Extraction, Item, Usage


def make_item(**over):
    base = dict(
        id="lw:abc123",
        source="lesswrong",
        site="lw",
        category="forums",
        url="https://www.lesswrong.com/posts/abc123/x",
        title="T",
        author="A",
        author_affiliation=None,
        published_at=datetime(2026, 8, 29, tzinfo=UTC),
        score=45,
        agreement=None,
        comment_count=3,
        curated=False,
        frontpage=True,
        author_created_at=None,
        text="body",
        markdown="body",
    )
    base.update(over)
    return Item(**base)


def test_item_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        make_item(bogus=1)


def test_extraction_importance_bounds():
    good = dict(
        item_id="x",
        summary="s",
        key_quotes=[],
        author_stated_status=None,
        claim_type="exploratory",
        novelty=None,
        discussion_signal=None,
        deadline=None,
        entities=[],
        slugs=["interp"],
        importance=3,
    )
    Extraction(**good)
    with pytest.raises(ValidationError):
        Extraction(**{**good, "importance": 6})
    with pytest.raises(ValidationError):
        Extraction(**{**good, "claim_type": "vibes"})


def _walk_schema(node):
    """Yield every dict node in a JSON schema tree, including the root."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_schema(value)


def test_extraction_schema_forbids_additional_properties():
    schema = Extraction.json_schema()
    assert schema["additionalProperties"] is False
    assert "item_id" in schema["required"]
    objects = [n for n in _walk_schema(schema) if n.get("type") == "object" and "properties" in n]
    assert objects, "expected at least the root object"
    for node in objects:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_extraction_schema_omits_api_unsupported_keywords():
    schema = Extraction.json_schema()
    unsupported = {
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
    for node in _walk_schema(schema):
        assert not (unsupported & node.keys()), f"unsupported keyword in {node}"


def test_extraction_still_enforces_constraints_client_side():
    good = dict(
        item_id="x",
        summary="s",
        key_quotes=[],
        author_stated_status=None,
        claim_type="exploratory",
        novelty=None,
        discussion_signal=None,
        deadline=None,
        entities=[],
        slugs=["interp"],
        importance=3,
    )
    with pytest.raises(ValidationError):
        Extraction(**{**good, "importance": 6})
    with pytest.raises(ValidationError):
        Extraction(**{**good, "key_quotes": ["a", "b", "c"]})


def test_slugs_include_review_additions():
    for slug in ["interp", "gov-evals", "cot-monitoring", "adjacent-xrisk", "culture", "careers"]:
        assert slug in SLUGS
    assert "aisi" not in SLUGS


def test_usage_adds():
    total = Usage(input_tokens=10, cost_usd=0.1) + Usage(output_tokens=5, cost_usd=0.2)
    assert (total.input_tokens, total.output_tokens) == (10, 5)
    assert total.cost_usd == pytest.approx(0.3)
