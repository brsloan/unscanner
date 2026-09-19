"""Backend for OpenAI-compatible chat endpoints: Ollama, vLLM, LM Studio, llama.cpp server, etc.

Uses plain HTTP (httpx) so there is no dependency on any vendor SDK. Needs a vision-capable model
(e.g. qwen2.5-vl, llama3.2-vision, gemma3, mistral-small3.1) served at <base_url>/chat/completions.
"""

from __future__ import annotations

import base64
import time

import httpx

from ..prompts import GUIDELINES, normalize_result, parse_model_json
from .base import Backend, BackendError, RefusalError, looks_like_refusal

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama
# The model GUIDELINES in prompts.py were tuned against; also what the UI Settings dialog pre-fills.
DEFAULT_MODEL = "qwen3.6:27b"
# Output budget per page; the UI setting "openai_max_tokens" / CLI --max-tokens override it. Even a
# dense page of tables is about 2000 tokens, so a page that hits this is nearly always a model stuck
# repeating itself. A bigger budget only makes that slower (and gateways answer 504 meanwhile).
DEFAULT_MAX_TOKENS = 8000
# A repetition loop under greedy decoding is broken by sampling: a truncated page is asked for again
# at these temperatures before it is reported as an error.
TRUNCATION_RETRY_TEMPERATURES = (0.3, 0.6)
MAX_ATTEMPTS = 7  # rate-limit back-off: 5, 10, 20, 40, 60, 60 s


def is_rate_limited(r: httpx.Response) -> bool:
    """429, or the 400 some gateways send with a 'rate limit' message."""
    return r.status_code == 429 or (r.status_code == 400 and "rate limit" in r.text.lower())


def normalize_base_url(url: str) -> str:
    """Return the API root that ``/chat/completions`` is appended to.

    People often paste the full endpoint from their provider's docs
    (``https://host/api/chat/completions``); accept that too instead of producing
    ``.../chat/completions/chat/completions`` and a 404/405.
    """
    url = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


class OpenAICompatBackend(Backend):
    name = "openai"

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS, temperature: float = 0.0, timeout: float = 600.0,
                 json_mode: bool = True, disable_thinking: bool = True):
        self.model = model or DEFAULT_MODEL
        self.base_url = normalize_base_url(base_url or DEFAULT_BASE_URL)
        self.api_key = api_key or "none"
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_mode = json_mode
        # Reasoning models (Qwen 3.x, DeepSeek, ...) think before answering by default and spend the
        # whole output budget on it for a page transcription. These two fields switch that off on
        # vLLM / SGLang / llama.cpp / Ollama; a server that rejects them gets a retry without.
        self.disable_thinking = disable_thinking
        self.client = httpx.Client(timeout=timeout)

    def _thinking_fields(self) -> dict:
        if not self.disable_thinking:
            return {}
        return {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}

    def transcribe(self, image_png: bytes, user_prompt: str) -> tuple[dict, dict]:
        img = base64.standard_b64encode(image_png).decode()
        body: dict = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": GUIDELINES},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                    {"type": "text", "text": user_prompt},
                ]},
            ],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self._thinking_fields())
        for temperature in (self.temperature, *TRUNCATION_RETRY_TEMPERATURES):
            body["temperature"] = temperature
            data = self._post(body)
            try:
                choice = data["choices"][0]
                msg = choice["message"]
                text = msg["content"]
            except (KeyError, IndexError, TypeError) as e:
                raise BackendError(f"unexpected response shape: {data!r}"[:500]) from e
            if choice.get("finish_reason") != "length":
                break
            if msg.get("reasoning_content") and not (text or "").strip():
                raise BackendError("model spent the whole output budget on reasoning (finish_reason=length); "
                                   "its thinking could not be switched off")
        else:
            raise BackendError(f"output truncated at {self.max_tokens} tokens on every try (finish_reason=length); "
                               "the model is probably repeating itself. Transcribe the page again, or raise "
                               "'Max output tokens' in Settings (CLI: --max-tokens) if the page really is that long")
        # OpenAI-style endpoints signal a refusal either with finish_reason="content_filter" or with a
        # separate "refusal" field on the message; smaller local models just answer in prose.
        if choice.get("finish_reason") == "content_filter" or msg.get("refusal"):
            raise RefusalError(f"model refused: {msg.get('refusal') or 'content filter'}")
        u = data.get("usage") or {}
        usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
                 "model": data.get("model", self.model)}
        try:
            return normalize_result(parse_model_json(text)), usage
        except ValueError as e:
            if looks_like_refusal(text):
                raise RefusalError(f"model refused: {(text or '').strip()[:300]}") from e
            raise BackendError(f"model returned invalid JSON: {e}") from e

    def _post(self, body: dict, network_tries: int = 3) -> dict:
        """POST one chat completion; handles rate limits, unsupported fields and transient errors.
        network_tries=1 suits a button a person is waiting on: an unreachable host fails at once."""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err: Exception | None = None
        data = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                if is_rate_limited(r):
                    last_err = BackendError(f"rate limited: {r.text.strip()[:200]}")
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                if r.status_code == 400 and self.json_mode and "response_format" in r.text:
                    self.json_mode = False  # server does not support JSON mode; rely on the prompt
                    body.pop("response_format", None)
                    continue
                if r.status_code == 400 and self.disable_thinking and (
                        "chat_template_kwargs" in r.text or "reasoning_effort" in r.text):
                    for k in self._thinking_fields():
                        body.pop(k, None)
                    self.disable_thinking = False  # server does not know these fields
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
                if attempt >= network_tries - 1:
                    break  # transient network errors get three tries; rate limits get MAX_ATTEMPTS
                time.sleep(3 * (attempt + 1))
        if data is None:
            if isinstance(last_err, httpx.ConnectError | httpx.ConnectTimeout):
                raise BackendError(f"cannot reach {self.base_url} (is this computer on the network or VPN the "
                                   f"endpoint needs?): {last_err}")
            raise BackendError(f"request failed: {last_err}")
        return data

    def describe_image(self, image_png: bytes, prompt: str, max_tokens: int | None = None) -> str:
        img = base64.standard_b64encode(image_png).decode()
        body = {
            "model": self.model, "temperature": self.temperature, "max_tokens": max_tokens or 600,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                {"type": "text", "text": prompt},
            ]}],
            **self._thinking_fields(),
        }
        data = self._post(body, network_tries=1)
        try:
            choice = data["choices"][0]
            text = (choice["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise BackendError(f"unexpected response shape: {data!r}"[:500]) from e
        if choice.get("finish_reason") == "length":
            raise BackendError(f"output truncated at {body['max_tokens']} tokens (finish_reason=length); the model "
                               "is probably repeating itself, try again")
        return text
