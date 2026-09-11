"""Model backends. Each exposes transcribe(image_png, user_prompt) -> (result_dict, usage_dict)."""

from __future__ import annotations

import os

from .base import Backend, BackendError


def make_backend(name: str | None = None, model: str | None = None, **kwargs) -> Backend:
    """Build a backend from explicit args or REMEDIATE_* environment variables.

    REMEDIATE_BACKEND   anthropic | openai        (default: anthropic)
    REMEDIATE_MODEL     model id for that backend
    REMEDIATE_OPENAI_BASE_URL / REMEDIATE_OPENAI_API_KEY   for the OpenAI-compatible backend
                        (Ollama: http://localhost:11434/v1, vLLM: http://host:8000/v1)
    """
    name = (name or os.environ.get("REMEDIATE_BACKEND") or "anthropic").lower()
    model = model or os.environ.get("REMEDIATE_MODEL") or None
    if name == "anthropic":
        from .anthropic_backend import AnthropicBackend

        return AnthropicBackend(model=model, **kwargs)
    if name in ("openai", "openai-compat", "ollama", "vllm"):
        from .openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(
            model=model,
            base_url=kwargs.pop("base_url", None) or os.environ.get("REMEDIATE_OPENAI_BASE_URL"),
            api_key=kwargs.pop("api_key", None) or os.environ.get("REMEDIATE_OPENAI_API_KEY"),
            **kwargs,
        )
    raise BackendError(f"unknown backend {name!r}; use 'anthropic' or 'openai'")


__all__ = ["Backend", "BackendError", "make_backend"]
