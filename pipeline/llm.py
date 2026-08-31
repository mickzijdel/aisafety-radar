"""The one place the pipeline talks to the Anthropic API.

Every call goes through `LLM`, so cost accounting, prompt caching and the refusal check happen
once rather than at each call site. `FakeLLM` replays canned responses in the same shape, which is
what the stage tests use: no test ever reaches the network.
"""

from __future__ import annotations

import json
from typing import Any

from pipeline.config import Settings
from pipeline.models import Usage


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
        price = self.settings.prices[model]
        cost = (
            u.input_tokens * price.input
            + cached * price.cache_read
            + u.output_tokens * price.output
        ) / 1_000_000
        self.usage = self.usage + Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=cached,
            cost_usd=cost,
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
        # Record before the refusal check: a refused turn is still billed for what it generated.
        self._record(response, model)
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError(f"{model} refused the request")
        return response

    @staticmethod
    def _first_text(response: Any) -> str:
        """Skip any thinking blocks, which precede the text block on a thinking response."""
        return next(b.text for b in response.content if b.type == "text")

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
        return json.loads(self._first_text(response))

    def text(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        thinking: bool = False,
    ) -> str:
        response = self._create(model, system, user, max_tokens, thinking=thinking, extra={})
        return self._first_text(response)

    def count_tokens(self, text: str, model: str | None = None) -> int:
        resp = self.client.messages.count_tokens(
            model=model or self.settings.models.haiku,
            messages=[{"role": "user", "content": text}],
        )
        return resp.input_tokens


class FakeLLM(LLM):
    """Replays `responses` in order and records what it was asked, for tests without a network.

    Deliberately does not call `super().__init__`: there is no client and no settings to price
    against, and a test that reaches a real client should fail loudly on the missing attribute.
    """

    def __init__(self, responses: list[dict[str, Any] | str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.usage = Usage()

    def _next(self, model: str, system: str, user: str) -> Any:
        self.calls.append({"model": model, "system": system, "user": user})
        if not self.responses:
            raise AssertionError("FakeLLM ran out of responses")
        return self.responses.pop(0)

    def structured(self, model, system, user, schema, max_tokens=2048):
        out = self._next(model, system, user)
        assert isinstance(out, dict), "structured() needs a dict response"
        return out

    def text(self, model, system, user, max_tokens=2048, thinking=False):
        out = self._next(model, system, user)
        assert isinstance(out, str), "text() needs a str response"
        return out

    def count_tokens(self, text, model=None):
        return len(text) // 4


def check_models(client: Any, settings: Settings) -> list[str]:
    """Return the configured ids the API still resolves; a short list means a retirement."""
    ok = []
    for model_id in (settings.models.haiku, settings.models.sonnet):
        try:
            client.models.retrieve(model_id)
            ok.append(model_id)
        except Exception:  # noqa: BLE001
            continue
    return ok
