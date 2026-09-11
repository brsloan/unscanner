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

    def describe_image(self, image_png: bytes, prompt: str) -> str:
        """Return plain text (e.g. alt text) for an image. Raise BackendError on failure."""
        raise BackendError(f"{self.name} backend cannot describe images")


ALT_TEXT_PROMPT = """\
Write alternative text for this image, which is a figure cut out of a scanned page of a course reading.
Describe what the image shows and what it conveys in context, in one to three plain sentences, so that
a reader who cannot see it gets the same information. Do not start with "Image of" or "Picture of".
Do not repeat the caption word for word. If the image is a purely decorative ornament or icon with no
informational content, answer with the single word DECORATIVE.
Return only the alternative text.
"""
