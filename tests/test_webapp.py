"""Web UI API tests with FastAPI's TestClient (no browser, no network)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from remediate import webapp
from remediate.sanitize import sanitize_fragment
from tests.test_pipeline import FakeBackend, make_pdf


@pytest.fixture
def client(tmp_path):
    app = webapp.create_app(tmp_path / "work", tmp_path / "out")
    with TestClient(app) as c:
        yield c


def test_sanitize_normalizes_editor_output():
    dirty = ('<div style="color:red"><b>Bold</b> and <i>it</i> <span class="x">plain</span> '
             '<span lang="fr">bonjour</span></div><div><br></div><script>x()</script>'
             '<p onclick="y()"><font face="a">text</font></p><img src="a.png"><a href="javascript:z()">l</a>')
    clean = sanitize_fragment(dirty)
    assert clean.startswith("<p><strong>Bold</strong> and <em>it</em> plain <span lang=\"fr\">bonjour</span></p>")
    assert "script" not in clean and "style=" not in clean and "onclick" not in clean and "font" not in clean
    assert '<img src="a.png" alt="">' in clean
    assert "javascript:" not in clean
    # block-wrapping div is unwrapped, not turned into a paragraph
    assert sanitize_fragment("<div><h2>T</h2><p>x</p></div>") == "<h2>T</h2><p>x</p>"


def test_open_edit_build_validate(client, tmp_path):
    pdf = make_pdf(tmp_path / "ui sample.pdf")
    r = client.post("/api/documents", json={"pdf_path": str(pdf), "title": "UI Sample"})
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]
    assert r.json()["page_count"] == 3

    assert client.get("/").status_code == 200 and "remediate" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200

    r = client.get(f"/api/documents/{doc_id}/pages/1")
    assert "CHAPTER ONE" in r.json()["draft_text"]
    img = client.get(f"/api/documents/{doc_id}/pages/1/image")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"

    for n, html in [(1, "<div><b>Chapter One</b></div><div>First page words here for coverage counts.</div>"),
                    (2, "<p>Second page words here for coverage counts.</p>"),
                    (3, "<p>Third.</p>")]:
        r = client.put(f"/api/documents/{doc_id}/pages/{n}", json={"html": html, "label": str(36 + n)})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "done"
    assert client.get(f"/api/documents/{doc_id}/pages/1").json()["html"].startswith("<p><strong>Chapter One</strong></p>")

    r = client.post(f"/api/documents/{doc_id}/build")
    assert r.status_code == 200 and r.json()["epub"]
    assert client.get(f"/api/documents/{doc_id}/output/html").status_code == 200
    assert client.get(f"/api/documents/{doc_id}/output/epub").headers["content-type"].startswith("application/epub")
    assert "<main" in client.get(f"/api/documents/{doc_id}/preview").text

    r = client.get(f"/api/documents/{doc_id}/validate", params={"epubcheck": "false"})
    assert r.status_code == 200
    assert not [i for i in r.json()["issues"] if i["severity"] == "error"], r.json()["issues"]


def test_settings_roundtrip(client):
    r = client.put("/api/settings", json={"backend": "openai", "openai_model": "qwen2.5vl:7b", "junk": 1})
    assert r.json()["backend"] == "openai" and "junk" not in r.json()
    assert client.get("/api/settings").json()["openai_model"] == "qwen2.5vl:7b"


def test_transcribe_job(client, tmp_path, monkeypatch):
    monkeypatch.setattr("remediate.backends.make_backend", lambda *a, **k: FakeBackend())
    pdf = make_pdf(tmp_path / "job sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    r = client.post(f"/api/documents/{doc_id}/transcribe", json={"pages": "all"})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "finished", j
    assert j["summary"]["done"] == 3
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["status_counts"].get("pending", 0) == 0
    assert d["pages"][1]["label"] == "38"
