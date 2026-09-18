"""Refusal handling: prompt context, optional title, refusal detection, per-page fallback backend."""

from __future__ import annotations

import json
import time

import anthropic
import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from unscanner import webapp
from unscanner.backends.anthropic_backend import AnthropicBackend
from unscanner.backends.base import Backend, BackendError, RefusalError, looks_like_refusal
from unscanner.backends.openai_compat import OpenAICompatBackend
from unscanner.document import Document
from unscanner.pdf import new_document
from unscanner.pipeline import transcribe_pages
from unscanner.prompts import CONTEXT, GUIDELINES, build_user_prompt
from tests.test_backends import RESULT
from tests.test_pipeline import FakeBackend, make_pdf


# ---------------------------------------------------------------- 1. prompt context

def test_guidelines_open_with_the_accessibility_context():
    assert GUIDELINES.startswith(CONTEXT)
    head = GUIDELINES[: len(CONTEXT) + 400]
    for phrase in ("accessibility service", "students with disabilities", "WCAG 2.1 AA",
                   "not being asked to recall", "correcting that OCR"):
        assert phrase in head, phrase
    # the contract itself is unchanged below the preamble
    assert "OUTPUT: a single JSON object" in GUIDELINES


# ---------------------------------------------------------------- 2. optional title

def test_user_prompt_can_leave_the_title_out():
    args = (3, 9, "draft", "", "", "A Famous Novel", "en")
    assert "Document: A Famous Novel (language: en)" in build_user_prompt(*args)
    without = build_user_prompt(*args, send_title=False)
    assert "A Famous Novel" not in without and "Document: untitled (language: en)" in without
    assert "This is PDF page 3 of 9." in without


# ---------------------------------------------------------------- 3. refusal detection

def test_looks_like_refusal_is_narrow():
    assert looks_like_refusal("I'm sorry, but I can't help with transcribing copyrighted material.")
    assert looks_like_refusal("I cannot transcribe this page as it appears to be from a copyrighted book.")
    assert not looks_like_refusal("")
    assert not looks_like_refusal("Here is the JSON: {\"html\": \"<p>x</p>\"")  # broken JSON, not a refusal
    # a long reply is a transcription that merely mentions copyright, never a refusal
    assert not looks_like_refusal("<p>The copyright notice reads " + "x" * 2000 + "</p>")


def _openai_backend(handler) -> OpenAICompatBackend:
    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    return be


def test_openai_compat_prose_refusal_is_a_refusal_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content":
            "I'm sorry, but I can't help with reproducing copyrighted material."}, "finish_reason": "stop"}]})

    with pytest.raises(RefusalError, match="refused"):
        _openai_backend(handler).transcribe(b"\x89PNG", "This is PDF page 1 of 1.")


def test_openai_compat_content_filter_and_refusal_field():
    def filtered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]})

    def refusal_field(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "refusal": "No."},
                                                      "finish_reason": "stop"}]})

    with pytest.raises(RefusalError):
        _openai_backend(filtered).transcribe(b"\x89PNG", "p")
    with pytest.raises(RefusalError, match="No."):
        _openai_backend(refusal_field).transcribe(b"\x89PNG", "p")


def test_openai_compat_garbage_is_still_a_plain_backend_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "<<not json at all>>"}, "finish_reason": "stop"}]})

    with pytest.raises(BackendError, match="invalid JSON") as ei:
        _openai_backend(handler).transcribe(b"\x89PNG", "p")
    assert not isinstance(ei.value, RefusalError)


def test_anthropic_refusal_stop_reason_is_a_refusal_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [], "stop_reason": "refusal", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0}})

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
                                 max_retries=0)
    be = AnthropicBackend(model="claude-haiku-4-5", client=client)
    with pytest.raises(RefusalError, match="refused"):
        be.transcribe(b"\x89PNG", "This is PDF page 1 of 1.")


# ---------------------------------------------------------------- 4. per-page fallback

class RefusingBackend(FakeBackend):
    """Behaves like FakeBackend except that it refuses the pages in `refuse` and records every prompt."""
    name = "refusing"
    model = "refuser-1"
    refuse = {2}

    def __init__(self):
        self.prompts: list[str] = []

    def transcribe(self, image_png: bytes, user_prompt: str):
        self.prompts.append(user_prompt)
        idx = int(user_prompt.split("This is PDF page ")[1].split(" ")[0])
        if idx in self.refuse:
            raise RefusalError("model refused: copyright")
        return super().transcribe(image_png, user_prompt)


@pytest.fixture
def doc(tmp_path):
    return new_document(make_pdf(tmp_path / "famous.pdf"), tmp_path / "work", title="A Famous Novel")


def test_refused_page_is_retried_on_the_fallback(doc):
    primary, fallback = RefusingBackend(), FakeBackend()
    summary = transcribe_pages(doc, primary, [1, 2, 3], fallback=fallback, workers=2)
    assert summary["done"] == 3 and summary["errors"] == 0
    assert summary["refused"] == 1 and summary["fell_back"] == 1
    assert summary["model"] == "refuser-1" and summary["fallback_model"] == "fake-1"
    doc = Document.load(doc.workdir)
    p = doc.page(2)
    assert p.label == "38" and "very long indeed" in p.html
    assert p.model == "fake-1" and p.changed_by == "fake-1"
    assert p.status == "needs_review"
    assert "refuser-1 refused this page" in p.notes and "transcribed by fake-1 instead" in p.notes
    assert doc.page(1).model == "refuser-1" and doc.page(1).status == "done"


def test_refusal_without_fallback_is_a_named_error(doc):
    summary = transcribe_pages(doc, RefusingBackend(), [1, 2, 3], workers=1)
    assert summary["done"] == 2 and summary["errors"] == 1
    assert summary["refused"] == 1 and summary["fell_back"] == 0 and summary["fallback_model"] is None
    p = Document.load(doc.workdir).page(2)
    assert p.status == "error" and p.notes.startswith("refuser-1 refused this page")


def test_fallback_that_also_fails_reports_both(doc):
    class Broken(FakeBackend):
        model = "broken-1"

        def transcribe(self, image_png, user_prompt):
            raise BackendError("connection error")

    summary = transcribe_pages(doc, RefusingBackend(), [2], fallback=Broken(), workers=1)
    assert summary["errors"] == 1 and summary["refused"] == 1 and summary["fell_back"] == 0
    notes = Document.load(doc.workdir).page(2).notes
    assert "refuser-1 refused this page" in notes and "fallback broken-1: connection error" in notes


def test_send_title_false_reaches_the_prompt(doc):
    primary = RefusingBackend()
    primary.refuse = set()
    transcribe_pages(doc, primary, [1], send_title=False, workers=1)
    assert primary.prompts and "A Famous Novel" not in primary.prompts[0]
    transcribe_pages(doc, primary, [1], force=True, workers=1)
    assert "A Famous Novel" in primary.prompts[-1]


# ---------------------------------------------------------------- web UI settings

@pytest.fixture
def client(tmp_path):
    app = webapp.create_app(tmp_path / "work", tmp_path / "out")
    with TestClient(app) as c:
        yield c


def test_settings_carry_fallback_and_title_flags(client):
    s = client.get("/api/settings").json()
    assert s["fallback_backend"] == "none" and s["send_title"] is True
    r = client.put("/api/settings", json={"fallback_backend": "openai", "send_title": False})
    assert r.json()["fallback_backend"] == "openai" and r.json()["send_title"] is False


def test_transcribe_job_uses_fallback_from_settings(client, tmp_path, monkeypatch):
    made: list[str] = []

    def fake_make_backend(name=None, model=None, **kw):
        made.append(name)
        return RefusingBackend() if name == "anthropic" else FakeBackend()

    monkeypatch.setattr("unscanner.backends.make_backend", fake_make_backend)
    client.put("/api/settings", json={"backend": "anthropic", "fallback_backend": "openai"})
    doc_id = client.post("/api/documents", json={"pdf_path": str(make_pdf(tmp_path / "job.pdf"))}).json()["doc_id"]
    r = client.post(f"/api/documents/{doc_id}/transcribe", json={"pages": "all"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "refuser-1" and r.json()["fallback_model"] == "fake-1"
    assert made == ["anthropic", "openai"]
    job_id = r.json()["id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "finished", j
    assert j["summary"]["done"] == 3 and j["summary"]["refused"] == 1 and j["summary"]["fell_back"] == 1
    pages = client.get(f"/api/documents/{doc_id}").json()["pages"]
    assert pages[1]["status"] == "needs_review" and "refused" in pages[1]["notes"]


def test_fallback_same_as_main_backend_is_ignored(client, tmp_path, monkeypatch):
    made: list[str] = []
    monkeypatch.setattr("unscanner.backends.make_backend", lambda name=None, model=None, **kw: (made.append(name), FakeBackend())[1])
    client.put("/api/settings", json={"backend": "openai", "fallback_backend": "openai"})
    doc_id = client.post("/api/documents", json={"pdf_path": str(make_pdf(tmp_path / "same.pdf"))}).json()["doc_id"]
    r = client.post(f"/api/documents/{doc_id}/transcribe", json={"pages": "1"})
    assert r.status_code == 200 and r.json()["fallback_model"] is None and made == ["openai"]
