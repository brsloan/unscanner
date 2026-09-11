"""Caret-to-scan location: word boxes from the PDF and the context matcher."""

from __future__ import annotations

from fastapi.testclient import TestClient

from remediate import webapp
from remediate.locate import locate, norm
from remediate.pdf import text_layer_words
from tests.test_pipeline import make_pdf

WORDS = [{"text": t, "x0": i * 50.0, "y0": 100.0, "x1": i * 50.0 + 40, "y1": 110.0}
         for i, t in enumerate("The quick brown fox jumps over the lazy dog and the quick red fox sleeps".split())]


def test_norm():
    assert norm("Hello,") == "hello" and norm("“quoted”") == "quoted" and norm("--") == ""


def test_locate_uses_context_to_disambiguate():
    # "quick" appears twice; the context decides which one.
    first = locate(WORDS, ["The", "quick", "brown", "fox"], 1)
    second = locate(WORDS, ["the", "quick", "red", "fox"], 1)
    assert first["text"] == "quick" and first["x0"] == 50.0
    assert second["text"] == "quick" and second["x0"] == 550.0


def test_locate_tolerates_hyphenation_and_punctuation():
    words = [{"text": t, "x0": i * 10.0, "y0": 0, "x1": i * 10.0 + 8, "y1": 5}
             for i, t in enumerate(["inter-", "national", "co-operation", "was", "expected,", "he", "said."])]
    r = locate(words, ["international", "cooperation", "was", "expected", "he"], 2)
    assert r is not None and r["text"] == "was"


def test_locate_rejects_weak_matches():
    assert locate(WORDS, ["nothing", "matches", "here", "at", "all"], 2) is None
    assert locate([], ["a"], 0) is None


def test_words_and_locate_endpoints(tmp_path):
    pdf = make_pdf(tmp_path / "loc.pdf")
    words = text_layer_words(pdf, 1)
    assert words and all(0 <= w["x0"] <= w["x1"] <= 1000 for w in words)
    assert any(w["text"] == "CHAPTER" for w in words)

    app = webapp.create_app(tmp_path / "work", tmp_path / "out", mount_mcp=False)
    with TestClient(app) as c:
        doc_id = c.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
        r = c.get(f"/api/documents/{doc_id}/pages/1/words")
        assert r.status_code == 200 and r.json()[0]["text"] == "CHAPTER"
        r = c.post(f"/api/documents/{doc_id}/pages/1/locate",
                   json={"context": ["the", "first", "paragraph", "of", "the", "chapter."], "index": 2})
        assert r.status_code == 200 and r.json()["found"] and r.json()["box"]["text"] == "paragraph"
        r = c.post(f"/api/documents/{doc_id}/pages/1/locate", json={"context": ["zzz", "yyy", "xxx"], "index": 1})
        assert r.json()["found"] is False
