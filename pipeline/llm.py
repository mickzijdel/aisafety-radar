"""The one place the pipeline talks to the Anthropic API.

Every call goes through `LLM`, so cost accounting, prompt caching and the refusal check happen
once rather than at each call site. `FakeLLM` replays canned responses in the same shape, which is
what the stage tests use: no test ever reaches the network. `CannedLLM` answers from the prompt
itself rather than from a script, which is what `radar run --dry-run` uses to exercise the whole
pipeline without an API key.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline.config import Settings
from pipeline.models import Usage

log = logging.getLogger(__name__)

# Four characters a token: the estimate `count_tokens` degrades to, and the one `FakeLLM` and
# `CannedLLM` answer with, so a degraded budget check is measured the same way the tests are.
CHARS_PER_TOKEN = 4


class LLM:
    def __init__(self, client: Any, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.usage = Usage()

    def _system_blocks(self, system: str) -> list[dict[str, Any]]:
        """One cached block: the system prompt is stable across a run, the user turn is not."""
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _record(self, response: Any, model: str) -> None:
        u = response.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        # The first call of a run writes the system prompt into the cache and is billed for it
        # at a premium over the input rate. Counted separately from `input_tokens`, which the
        # API already reports net of both cache fields.
        written = getattr(u, "cache_creation_input_tokens", 0) or 0
        price = self.settings.prices[model]
        cost = (
            u.input_tokens * price.input
            + cached * price.cache_read
            + written * price.cache_write
            + u.output_tokens * price.output
        ) / 1_000_000
        self.usage = self.usage + Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=cached,
            cache_creation_tokens=written,
            cost_usd=cost,
        )

    def _check_thinking(self, model: str, thinking: bool) -> None:
        """Refuse a thinking parameter on any model but Sonnet, before the call is made.

        Enforced here rather than at each call site: only Sonnet takes one, and Haiku rejects one
        outright (CLAUDE.md, spec 4). `FakeLLM` reuses this, so a stage test cannot ask for a
        combination the real client would reject and stay green.
        """
        if thinking and model != self.settings.models.sonnet:
            raise ValueError(
                f"thinking is only available on {self.settings.models.sonnet}, not {model}"
            )

    def _create(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        thinking: bool,
        extra: dict[str, Any],
    ) -> Any:
        self._check_thinking(model, thinking)
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=self._system_blocks(system),
            messages=[{"role": "user", "content": user}],
            **extra,
        )
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {**kwargs.get("output_config", {}), "effort": "medium"}
        response = self.client.messages.create(**kwargs)
        # Record before the failure checks: a refused or truncated turn is still billed for what
        # it generated.
        self._record(response, model)
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise RuntimeError(f"{model} refused the request")
        if stop_reason == "max_tokens":
            # Caught here so a truncated structured response never reaches json.loads, where it
            # would look like a malformed reply rather than a budget that was too small.
            raise RuntimeError(f"{model} response truncated at max_tokens")
        return response

    @staticmethod
    def _first_text(response: Any, model: str) -> str:
        """Skip any thinking blocks, which precede the text block on a thinking response."""
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            stop_reason = getattr(response, "stop_reason", None)
            raise RuntimeError(f"{model} returned no text block (stop_reason={stop_reason})")
        return text

    def structured(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        response = self._create(
            model,
            system,
            user,
            max_tokens,
            thinking=False,
            extra={"output_config": {"format": {"type": "json_schema", "schema": schema}}},
        )
        return json.loads(self._first_text(response, model))

    def text(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        thinking: bool = False,
    ) -> str:
        response = self._create(model, system, user, max_tokens, thinking=thinking, extra={})
        return self._first_text(response, model)

    def count_tokens(self, text: str, model: str | None = None) -> int:
        """The API's token count for `text`, or a four-characters-a-token estimate if it fails.

        This runs at render time, after the day's extraction and judging are already paid for,
        and it feeds nothing but the budget gate's comparison against a per-file token cap. A
        transient failure here must therefore not raise: it would throw away a fully extracted
        day for a number that only has to be approximately right.
        """
        try:
            resp = self.client.messages.count_tokens(
                model=model or self.settings.models.haiku,
                messages=[{"role": "user", "content": text}],
            )
        except Exception as exc:  # noqa: BLE001 - a lost count is not a lost day
            log.warning("count_tokens failed (%s); estimating from the character count", exc)
            return len(text) // CHARS_PER_TOKEN
        return resp.input_tokens


class FakeLLM(LLM):
    """Replays `responses` in order and records what it was asked, for tests without a network.

    Deliberately does not call `super().__init__`: there is no client to price against, and a
    test that reaches a real client should fail loudly on the missing attribute.

    `settings` is optional and carries exactly one thing: with it, `text` applies the same
    thinking guard the real client would, so a stage test cannot pass `thinking=True` on Haiku
    and stay green against a call the API rejects.
    """

    def __init__(
        self,
        responses: list[dict[str, Any] | str] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.usage = Usage()
        self.settings = settings

    def _next(self, model: str, system: str, user: str, **extra: Any) -> Any:
        """Record the call, then hand back the next canned response.

        Every entry carries model, system and user; `structured` adds `schema` and `max_tokens`,
        `text` adds `max_tokens` and `thinking`, so a stage test can assert the whole contract.
        """
        self.calls.append({"model": model, "system": system, "user": user, **extra})
        if not self.responses:
            raise AssertionError("FakeLLM ran out of responses")
        return self.responses.pop(0)

    def structured(self, model, system, user, schema, max_tokens=2048):
        out = self._next(model, system, user, schema=schema, max_tokens=max_tokens)
        assert isinstance(out, dict), "structured() needs a dict response"
        return out

    def text(self, model, system, user, max_tokens=2048, thinking=False):
        if self.settings is not None:
            self._check_thinking(model, thinking)
        out = self._next(model, system, user, max_tokens=max_tokens, thinking=thinking)
        assert isinstance(out, str), "text() needs a str response"
        return out

    def count_tokens(self, text, model=None):
        return len(text) // CHARS_PER_TOKEN


class CannedLLM(LLM):
    """Deterministic stand-in for dry runs without an API key.

    Answers are dispatched on the schema's property names rather than on the model id, so every
    stage gets a plausible reply and the whole pipeline runs end to end for the price of nothing.
    The extraction answer is copied out of the source document, which is what makes a dry run
    worth reading: the substring verifier passes on it, so items render with a summary and a
    quote instead of degrading to "summary withheld".

    Like `FakeLLM` it deliberately skips `super().__init__`: there is no client to price against,
    and it records every call the same way, so a test can assert on the wiring.
    """

    def __init__(self) -> None:
        self.usage = Usage()
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _block(user: str, tag: str) -> str:
        """The body of the `<tag ...>` block in `user`, or the whole message if there is none."""
        start = user.find(f"<{tag}")
        end = user.find(f"</{tag}>")
        if start == -1 or end == -1:
            return user
        body = user[start:end]
        return body.split(">", 1)[1] if ">" in body else body

    @classmethod
    def _document(cls, user: str) -> str:
        """The source document's own text, without the header `build_user_message` puts above it.

        That header carries the title, the author and the publication date, and a summary built
        from it would state a date the document itself does not contain -- which `verify` catches,
        degrading every item in the dry run. A post with no text of its own therefore comes back
        empty rather than as its own metadata, and the caller answers "No text." to that. A block
        that does not carry the header at all is used whole, so this stays a formatting detail.
        """
        body = cls._block(user, "source_document").strip()
        if not body.startswith("title:"):
            return body
        _, _, text = body.partition("\n\n")
        return text.strip()

    def structured(self, model, system, user, schema, max_tokens=2048):
        self.calls.append({"model": model, "system": system, "user": user})
        props = set(schema.get("properties", {}))
        if {"relevant", "primary_slug"} <= props:
            return {
                "item_id": "",
                "relevant": True,
                "primary_slug": "community",
                "secondary_slugs": [],
                "importance": 3,
            }
        if {"summary", "key_quotes"} <= props:
            doc = self._document(user)
            sentences = [s.strip() for s in doc.replace("\n", " ").split(". ") if s.strip()]
            body = ". ".join(sentences[:2])[:400]
            words = doc.split()
            quote = " ".join(words[:8]) if len(words) >= 8 else ""
            return {
                "item_id": "",
                "summary": body or "No text.",
                "key_quotes": [quote] if quote else [],
                "author_stated_status": None,
                "claim_type": "exploratory",
                "novelty": None,
                "discussion_signal": None,
                "deadline": None,
                "entities": [],
                "slugs": ["community"],
                "importance": 3,
            }
        if "flagged" in props:
            return {"flagged": False, "reason": None}
        if "traceable" in props:
            return {"traceable": True, "status_matches": True, "dense": True, "notes": "canned"}
        if "discussion_signal" in props:
            return {"discussion_signal": None}
        raise AssertionError(f"CannedLLM does not know this schema: {sorted(props)}")

    def text(self, model, system, user, max_tokens=2048, thinking=False):
        self.calls.append({"model": model, "system": system, "user": user})
        return "canned text"

    def count_tokens(self, text, model=None):
        return len(text) // CHARS_PER_TOKEN


def check_models(client: Any, settings: Settings) -> list[str]:
    """Return the configured ids the API still resolves; a short list means a retirement."""
    ok = []
    for model_id in (settings.models.haiku, settings.models.sonnet):
        try:
            client.models.retrieve(model_id)
            ok.append(model_id)
        except Exception:  # noqa: BLE001, S112 - an unresolved model is left out
            continue
    return ok
