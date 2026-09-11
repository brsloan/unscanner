"""Claude backend via the official Anthropic SDK (vision + structured JSON output)."""

from __future__ import annotations

import base64
import time

import anthropic

from ..prompts import GUIDELINES, OUTPUT_SCHEMA, normalize_result, parse_model_json
from .base import Backend, BackendError

DEFAULT_MODEL = "claude-opus-5"
# Models that accept output_config.effort (Haiku 4.5 and older models reject it).
_EFFORT_MODELS = ("claude-opus-5", "claude-opus-4", "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable")
# Models where server-side refusal fallbacks are worth enabling.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable")


class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str = "medium", max_tokens: int = 16000,
                 fallbacks: bool = True, client: anthropic.Anthropic | None = None):
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.max_tokens = max_tokens
        self.use_fallbacks = fallbacks and self.model.startswith(_FALLBACK_MODELS)
        # Credentials resolve from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile.
        self.client = client or anthropic.Anthropic()

    def _request_kwargs(self, image_png: bytes, user_prompt: str) -> dict:
        img = base64.standard_b64encode(image_png).decode()
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            # Stable system prompt first so it is served from the prompt cache on every page.
            system=[{"type": "text", "text": GUIDELINES, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                    {"type": "text", "text": user_prompt},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        if self.model.startswith(_EFFORT_MODELS):
            kwargs["output_config"]["effort"] = self.effort
        return kwargs

    def _create(self, kwargs: dict):
        if self.use_fallbacks:
            try:
                return self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            except anthropic.BadRequestError:
                # Fallbacks unavailable on this endpoint/account: continue without them.
                self.use_fallbacks = False
        return self.client.messages.create(**kwargs)

    def transcribe(self, image_png: bytes, user_prompt: str) -> tuple[dict, dict]:
        kwargs = self._request_kwargs(image_png, user_prompt)
        resp = None
        for attempt in range(4):
            try:
                resp = self._create(kwargs)
                break
            except anthropic.RateLimitError as e:
                wait = int(e.response.headers.get("retry-after", "20"))
                time.sleep(min(wait, 120))
            except anthropic.APIStatusError as e:
                if e.status_code >= 500 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise BackendError(f"Anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise BackendError(f"connection error: {e}") from e
        if resp is None:
            raise BackendError("gave up after repeated rate limiting")

        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            raise BackendError(f"model refused: {getattr(detail, 'explanation', '') or 'no explanation'}")
        if resp.stop_reason == "max_tokens":
            raise BackendError("output truncated at max_tokens; raise max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            "model": resp.model,
        }
        try:
            return normalize_result(parse_model_json(text)), usage
        except ValueError as e:
            raise BackendError(f"model returned invalid JSON: {e}") from e
