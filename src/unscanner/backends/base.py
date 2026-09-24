from __future__ import annotations

from abc import ABC, abstractmethod


class BackendError(RuntimeError):
    pass


class RefusalError(BackendError):
    """The model declined to transcribe the page (a safety/copyright refusal, not a technical failure).

    The pipeline treats this differently from other errors: it retries the page on the fallback
    backend when one is configured, and records the refusal in the page notes so a person knows to
    do that page by hand rather than chase a parsing bug.
    """


# Phrases that mark a refusal when a model answers in prose instead of the JSON we asked for.
# Kept deliberately narrow: they must not match transcribed body text or a legitimate "notes" field.
_REFUSAL_MARKERS = (
    "i can't help with", "i cannot help with", "i can't assist with", "i cannot assist with",
    "i'm not able to help with", "i am not able to help with", "i'm unable to help with",
    "i can't transcribe", "i cannot transcribe", "i can't reproduce", "i cannot reproduce",
    "i can't provide", "i cannot provide", "i won't be able to", "i'm sorry, but i can't",
    "i'm sorry, but i cannot", "unable to comply", "against my guidelines", "copyright",
    "copyrighted material", "copyrighted work", "intellectual property",
)


def looks_like_refusal(text: str) -> bool:
    """True when a raw model reply reads as a refusal rather than a transcription result.

    Only meant for replies that are NOT valid JSON (a JSON result with the word "copyright" in a
    transcribed paragraph is fine). Short prose containing a refusal phrase counts; long text does
    not, since a real transcription is never a refusal.
    """
    t = (text or "").strip().lower()
    if not t or len(t) > 1500:
        return False
    return any(m in t for m in _REFUSAL_MARKERS)


class Backend(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def transcribe(self, image_png: bytes, user_prompt: str, system: str | None = None) -> tuple[dict, dict]:
        """Return (normalized result dict, usage dict). Raise BackendError on unrecoverable failure.
        system is the system prompt (prompts.guidelines() with the library's edits); None means the built-in one."""

    def describe_image(self, image_png: bytes, prompt: str, max_tokens: int | None = None) -> str:
        """Return the model's plain-text answer about an image (alt text, or a region re-read as a
        table). max_tokens=None keeps the small budget that suits alt text. Raise BackendError on failure."""
        raise BackendError(f"{self.name} backend cannot describe images")


ALT_TEXT_PROMPT = """\
Write alternative text for this image, which is a figure cut out of a scanned page of a course reading.
Describe what the image shows and what it conveys in context, in one to three plain sentences, so that
a reader who cannot see it gets the same information. Do not start with "Image of" or "Picture of".
Do not repeat the caption word for word. If the image is a purely decorative ornament or icon with no
informational content, answer with the single word DECORATIVE.
Return only the alternative text.
"""
