from __future__ import annotations

from abc import ABC, abstractmethod


class BackendError(RuntimeError):
    pass


class Backend(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def transcribe(self, image_png: bytes, user_prompt: str) -> tuple[dict, dict]:
        """Return (normalized result dict, usage dict). Raise BackendError on unrecoverable failure."""
