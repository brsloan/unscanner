"""Backend for OpenAI-compatible chat endpoints: Ollama, vLLM, LM Studio, llama.cpp server, etc.

Uses plain HTTP (httpx) so there is no dependency on any vendor SDK. Needs a vision-capable model
(e.g. qwen2.5-vl, llama3.2-vision, gemma3, mistral-small3.1) served at <base_url>/chat/completions.
"""

from __future__ import annotations

import base64
import time

import httpx

from ..prompts import GUIDELINES, normalize_result, parse_model_json
from .base import Backend, BackendError

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama
DEFAULT_MODEL = "qwen2.5vl:7b"


class OpenAICompatBackend(Backend):
    name = "openai"

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 max_tokens: int = 8000, temperature: float = 0.0, timeout: float = 600.0,
                 json_mode: bool = True):
        self.model = model or DEFAULT_MODEL
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or "none"
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_mode = json_mode
        self.client = httpx.Client(timeout=timeout)

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
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err: Exception | None = None
        data = None
        for attempt in range(3):
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                if r.status_code == 400 and self.json_mode and "response_format" in r.text:
                    self.json_mode = False  # server does not support JSON mode; rely on the prompt
                    body.pop("response_format", None)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
                time.sleep(3 * (attempt + 1))
        if data is None:
            raise BackendError(f"request failed: {last_err}")
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise BackendError(f"unexpected response shape: {data!r}"[:500]) from e
        if choice.get("finish_reason") == "length":
            raise BackendError("output truncated (finish_reason=length); raise max_tokens")
        u = data.get("usage") or {}
        usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
                 "model": data.get("model", self.model)}
        try:
            return normalize_result(parse_model_json(text)), usage
        except ValueError as e:
            raise BackendError(f"model returned invalid JSON: {e}") from e
