import json
from types import SimpleNamespace

import pytest

from pipeline.config import load_settings
from pipeline.llm import LLM, FakeLLM, check_models


class StubMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps({"ok": True}))],
            usage=SimpleNamespace(
                input_tokens=1000, output_tokens=100, cache_read_input_tokens=500
            ),
            stop_reason="end_turn",
        )

    def count_tokens(self, **kw):
        return SimpleNamespace(input_tokens=42)


@pytest.fixture
def stub():
    return SimpleNamespace(messages=StubMessages())


def test_structured_sends_schema_and_records_cost(stub):
    s = load_settings()
    llm = LLM(stub, s)
    out = llm.structured(s.models.haiku, "SYS", "USER", {"type": "object"})
    assert out == {"ok": True}
    call = stub.messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in call
    # 1000 uncached in at $1/M, 500 cached at $0.1/M, 100 out at $5/M
    assert llm.usage.cost_usd == pytest.approx(0.001 + 0.00005 + 0.0005)
    assert llm.usage.input_tokens == 1000 and llm.usage.cache_read_tokens == 500


def test_text_with_thinking_on_sonnet(stub):
    s = load_settings()
    llm = LLM(stub, s)
    llm.text(s.models.sonnet, "SYS", "USER", thinking=True)
    call = stub.messages.calls[0]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}


def test_count_tokens(stub):
    assert LLM(stub, load_settings()).count_tokens("hello") == 42


def test_fake_llm_replays_in_order():
    fake = FakeLLM([{"a": 1}, "some text"])
    assert fake.structured("m", "s", "u", {}) == {"a": 1}
    assert fake.text("m", "s", "u") == "some text"
    assert fake.calls[0]["user"] == "u" and fake.count_tokens("abcd" * 10) == 10


def test_usage_accumulates_across_calls(stub):
    s = load_settings()
    llm = LLM(stub, s)
    llm.text(s.models.haiku, "SYS", "USER")
    llm.text(s.models.haiku, "SYS", "USER")
    assert llm.usage.input_tokens == 2000 and llm.usage.output_tokens == 200
    assert llm.usage.cost_usd == pytest.approx(2 * (0.001 + 0.00005 + 0.0005))


def test_sonnet_is_priced_from_its_own_row(stub):
    s = load_settings()
    llm = LLM(stub, s)
    llm.text(s.models.sonnet, "SYS", "USER")
    # Sonnet is $2/M in, $0.2/M cached, $10/M out.
    assert llm.usage.cost_usd == pytest.approx(0.002 + 0.0001 + 0.001)


def test_text_without_thinking_passes_neither_thinking_nor_effort(stub):
    s = load_settings()
    LLM(stub, s).text(s.models.haiku, "SYS", "USER", max_tokens=64)
    call = stub.messages.calls[0]
    assert "thinking" not in call and "output_config" not in call
    assert call["max_tokens"] == 64
    assert call["messages"] == [{"role": "user", "content": "USER"}]


def test_refusal_raises_and_still_records_usage(stub):
    def refuse(**kw):
        stub.messages.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=1, cache_read_input_tokens=0),
            stop_reason="refusal",
        )

    stub.messages.create = refuse
    s = load_settings()
    llm = LLM(stub, s)
    with pytest.raises(RuntimeError, match="refused"):
        llm.text(s.models.haiku, "SYS", "USER")
    assert llm.usage.input_tokens == 10


def test_usage_survives_a_response_without_cache_fields(stub):
    stub.messages.create = lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=10),
        stop_reason="end_turn",
    )
    s = load_settings()
    llm = LLM(stub, s)
    assert llm.text(s.models.haiku, "SYS", "USER") == "hi"
    assert llm.usage.cache_read_tokens == 0


def test_first_text_skips_thinking_blocks(stub):
    stub.messages.create = lambda **kw: SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="..."),
            SimpleNamespace(type="text", text="answer"),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
        stop_reason="end_turn",
    )
    s = load_settings()
    assert LLM(stub, s).text(s.models.sonnet, "SYS", "USER", thinking=True) == "answer"


def test_count_tokens_defaults_to_haiku_and_honours_an_override(stub):
    seen = []

    def count_tokens(**kw):
        seen.append(kw)
        return SimpleNamespace(input_tokens=7)

    stub.messages.count_tokens = count_tokens
    s = load_settings()
    llm = LLM(stub, s)
    assert llm.count_tokens("hello") == 7
    assert llm.count_tokens("hello", model=s.models.sonnet) == 7
    assert [kw["model"] for kw in seen] == [s.models.haiku, s.models.sonnet]


def test_fake_llm_records_every_call_and_runs_dry():
    fake = FakeLLM(["one"])
    fake.text("m", "sys", "u")
    assert fake.calls == [
        {"model": "m", "system": "sys", "user": "u", "max_tokens": 2048, "thinking": False}
    ]
    with pytest.raises(AssertionError):
        fake.text("m", "sys", "u")


def test_fake_llm_records_the_full_call_contract():
    """Stage tests assert on the schema, budget and thinking flag a stage chose."""
    fake = FakeLLM([{"a": 1}, "text"])
    fake.structured("m", "sys", "u", {"type": "object"}, max_tokens=64)
    fake.text("m2", "sys2", "u2", max_tokens=128, thinking=True)
    assert fake.calls[0] == {
        "model": "m",
        "system": "sys",
        "user": "u",
        "schema": {"type": "object"},
        "max_tokens": 64,
    }
    assert fake.calls[1] == {
        "model": "m2",
        "system": "sys2",
        "user": "u2",
        "max_tokens": 128,
        "thinking": True,
    }


def test_fake_llm_rejects_a_mistyped_response():
    with pytest.raises(AssertionError):
        FakeLLM(["not a dict"]).structured("m", "s", "u", {})
    with pytest.raises(AssertionError):
        FakeLLM([{"a": 1}]).text("m", "s", "u")


def _responder(stub, *, content, stop_reason):
    def create(**kw):
        stub.messages.calls.append(kw)
        return SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0),
            stop_reason=stop_reason,
        )

    stub.messages.create = create


def test_no_text_block_raises_a_named_error(stub):
    """Adaptive thinking can eat the whole budget and return no text block."""
    _responder(stub, content=[], stop_reason="end_turn")
    s = load_settings()
    llm = LLM(stub, s)
    with pytest.raises(RuntimeError, match="no text block .*stop_reason=end_turn"):
        llm.text(s.models.haiku, "SYS", "USER")
    assert llm.usage.input_tokens == 1000


def test_thinking_only_content_raises_rather_than_stopiteration(stub):
    _responder(
        stub, content=[SimpleNamespace(type="thinking", thinking="...")], stop_reason="end_turn"
    )
    s = load_settings()
    with pytest.raises(RuntimeError, match="no text block"):
        LLM(stub, s).text(s.models.sonnet, "SYS", "USER", thinking=True)


def test_truncation_raises_before_json_parsing(stub):
    """A response cut off at max_tokens must not reach json.loads as a decode error."""
    _responder(
        stub,
        content=[SimpleNamespace(type="text", text='{"ok": tr')],
        stop_reason="max_tokens",
    )
    s = load_settings()
    llm = LLM(stub, s)
    with pytest.raises(RuntimeError, match="truncated at max_tokens"):
        llm.structured(s.models.haiku, "SYS", "USER", {"type": "object"})
    with pytest.raises(RuntimeError, match="truncated at max_tokens"):
        llm.text(s.models.haiku, "SYS", "USER")
    # Both truncated turns are still billed.
    assert llm.usage.input_tokens == 2000


def test_thinking_on_haiku_is_refused_before_the_call(stub):
    s = load_settings()
    llm = LLM(stub, s)
    with pytest.raises(ValueError, match="thinking"):
        llm.text(s.models.haiku, "SYS", "USER", thinking=True)
    assert stub.messages.calls == []


def test_check_models_returns_only_the_ids_that_resolve():
    s = load_settings()
    retrieved = []

    def retrieve(model_id):
        retrieved.append(model_id)
        if model_id == s.models.sonnet:
            raise RuntimeError("not found")
        return SimpleNamespace(id=model_id)

    client = SimpleNamespace(models=SimpleNamespace(retrieve=retrieve))
    assert check_models(client, s) == [s.models.haiku]
    assert retrieved == [s.models.haiku, s.models.sonnet]


def test_cache_writes_are_priced_and_recorded(stub):
    """Every call writes the system prompt into the cache the first time it is seen.

    A cache write is billed at 1.25x the input rate, and leaving it out of the arithmetic
    understates the day's cost against the figure the README and spec 6.10 quote.
    """
    stub.messages.create = lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=800,
        ),
        stop_reason="end_turn",
    )
    s = load_settings()
    llm = LLM(stub, s)
    llm.text(s.models.haiku, "SYS", "USER")
    assert llm.usage.cache_creation_tokens == 800
    # 1000 in at $1/M, 500 cached-read at $0.1/M, 800 cache-write at $1.25/M, 100 out at $5/M.
    assert llm.usage.cost_usd == pytest.approx(0.001 + 0.00005 + 0.001 + 0.0005)


def test_usage_survives_a_response_without_a_cache_write_field(stub):
    s = load_settings()
    llm = LLM(stub, s)
    llm.text(s.models.haiku, "SYS", "USER")
    assert llm.usage.cache_creation_tokens == 0


def test_cache_write_is_priced_from_the_calls_own_model(stub):
    stub.messages.create = lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=1_000_000,
        ),
        stop_reason="end_turn",
    )
    s = load_settings()
    llm = LLM(stub, s)
    llm.text(s.models.sonnet, "SYS", "USER")
    assert llm.usage.cost_usd == pytest.approx(2.50)


def test_count_tokens_falls_back_to_an_estimate_when_the_api_fails(stub, caplog):
    """`count_tokens` runs at render time, after the day's extraction is paid for.

    A transient failure there would throw away a fully extracted day for a number the budget
    gate only needs approximately, so it degrades to the same four-characters-a-token estimate
    `FakeLLM` uses and says so in the log.
    """

    def boom(**kw):
        raise RuntimeError("upstream 529")

    stub.messages.count_tokens = boom
    s = load_settings()
    with caplog.at_level("WARNING", logger="pipeline.llm"):
        assert LLM(stub, s).count_tokens("a" * 400) == 100
    assert "upstream 529" in caplog.text


def test_fake_llm_enforces_the_thinking_guard_when_it_is_given_settings():
    """A stage test that asks for thinking on Haiku must fail here, not in production.

    The guard is the real `LLM`'s, reused rather than restated: a fake that is more permissive
    than the client it stands in for is a fake that certifies calls the API rejects.
    """
    s = load_settings()
    fake = FakeLLM(["answer"], settings=s)
    with pytest.raises(ValueError, match="thinking"):
        fake.text(s.models.haiku, "SYS", "USER", thinking=True)
    assert fake.calls == [], "the guard must refuse before the call is recorded"
    assert fake.text(s.models.sonnet, "SYS", "USER", thinking=True) == "answer"


def test_fake_llm_without_settings_still_takes_any_model():
    """Most stage tests never send thinking at all, so settings stays optional."""
    fake = FakeLLM(["answer"])
    assert fake.text("any-model", "SYS", "USER", thinking=True) == "answer"
