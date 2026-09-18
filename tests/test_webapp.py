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
    # an image-only paragraph (how browsers wrap an inserted image) becomes a figure, classes kept
    assert sanitize_fragment('<p class="align-right wrap"><img src="fig:9-1" alt="x"><br></p>') == \
        '<figure class="align-right wrap"><img src="fig:9-1" alt="x"></figure>'
    assert sanitize_fragment('<p>caption <img src="fig:9-1" alt="x"></p>').startswith("<p>caption ")
    # a heading that was centered in print keeps align-center; other classes and inline styles go
    assert sanitize_fragment('<h2 class="align-center big" style="color:red">T</h2>') == '<h2 class="align-center">T</h2>'
    # the editor's alignment buttons put the same classes on paragraphs
    assert sanitize_fragment('<p class="align-right" style="text-align:right">x</p>') == '<p class="align-right">x</p>'


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


def test_save_versus_approve(client, tmp_path):
    pdf = make_pdf(tmp_path / "approve sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    # Save (draft) keeps the page unapproved; Approve marks it done; editing again and saving un-approves it.
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>draft text</p>", "status": "needs_review"})
    assert r.json()["status"] == "needs_review"
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>draft text</p>", "status": "done", "version": 1})
    assert r.json()["status"] == "done"
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>changed</p>", "status": "needs_review", "version": 2})
    assert r.json()["status"] == "needs_review"


def test_figures_survive_editor_saves_and_can_be_previewed(client, tmp_path):
    pdf = make_pdf(tmp_path / "fig sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    html = '<figure><img src="fig:1-1" alt="A grey box."></figure><p>Text here for the page.</p>'
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={
        "html": html, "figures": [{"id": "1-1", "alt": "A grey box.", "bbox": [100, 100, 300, 300], "caption": ""}]})
    assert r.status_code == 200
    # A later save from the editor (no figures field) keeps the crop box...
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={"html": html.replace("Text", "More text"), "version": 1})
    assert r.status_code == 200
    fig = client.get(f"/api/documents/{doc_id}/pages/1/figure/1-1")
    assert fig.status_code == 200 and fig.content[:4] == b"\x89PNG"
    assert client.get(f"/api/documents/{doc_id}/pages/1/figure/nope").status_code == 404
    # live preview of an adjusted crop, and the adjusted box persisted by a save that sends figures
    live = client.get(f"/api/documents/{doc_id}/pages/1/figure/1-1", params={"bbox": "0,0,500,500"})
    assert live.status_code == 200 and live.content[:4] == b"\x89PNG"
    assert client.get(f"/api/documents/{doc_id}/pages/1/figure/1-1", params={"bbox": "9,9,1"}).status_code == 400
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={
        "html": html, "version": 2, "figures": [{"id": "1-1", "alt": "A grey box.", "bbox": [50, 60, 400, 420], "caption": ""}]})
    assert r.status_code == 200
    assert client.get(f"/api/documents/{doc_id}/pages/1").json()["figures"][0]["bbox"] == [50, 60, 400, 420]
    # ...and the build crops it and the HTML references the file.
    r = client.post(f"/api/documents/{doc_id}/build")
    assert r.json()["figures"] == 1
    assert 'src="figures/p001-1-1.png"' in client.get(f"/api/documents/{doc_id}/preview").text


def test_figure_alt_autofill_and_layout_classes(client, tmp_path, monkeypatch):
    class DescribingBackend(FakeBackend):
        def describe_image(self, image_png, prompt):
            assert image_png[:4] == b"\x89PNG" and "Caption printed with the image: Figure 1" in prompt
            return "A grey rectangle standing in for a chart."

    monkeypatch.setattr("remediate.backends.make_backend", lambda *a, **k: DescribingBackend())
    pdf = make_pdf(tmp_path / "alt sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    client.put(f"/api/documents/{doc_id}/pages/1", json={
        "html": '<figure class="align-right wrap junk" style="x"><img src="fig:1-1" alt=""><figcaption>Figure 1</figcaption></figure><p>Body.</p>',
        "figures": [{"id": "1-1", "alt": "", "bbox": [100, 100, 300, 300], "caption": "Figure 1"}]})
    page = client.get(f"/api/documents/{doc_id}/pages/1").json()
    assert '<figure class="align-right wrap">' in page["html"]  # layout classes kept, junk dropped
    r = client.post(f"/api/documents/{doc_id}/pages/1/figures/1-1/describe", json={"caption": "Figure 1", "context": "Body."})
    assert r.status_code == 200 and r.json()["alt"].startswith("A grey rectangle") and r.json()["decorative"] is False
    assert client.post(f"/api/documents/{doc_id}/pages/1/figures/zzz/describe", json={}).status_code == 404
    # the built HTML keeps the placement classes and carries CSS for them
    client.post(f"/api/documents/{doc_id}/build")
    out = client.get(f"/api/documents/{doc_id}/preview").text
    assert 'class="align-right wrap"' in out and "figure.wrap.align-right" in out


def test_settings_roundtrip(client):
    # untouched settings pre-fill the model the guidelines were tuned against
    assert client.get("/api/settings").json()["openai_model"] == "qwen3.6:27b"
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


def test_settings_max_tokens_reaches_openai_backend(client, tmp_path, monkeypatch):
    import remediate.backends as backends

    seen = {}

    def fake_make_backend(name=None, model=None, **kw):
        seen.update(kw, name=name)
        raise backends.BackendError("stop here")

    monkeypatch.setattr(backends, "make_backend", fake_make_backend)
    assert client.get("/api/settings").json()["openai_max_tokens"] == 8000
    client.put("/api/settings", json={"backend": "openai", "openai_max_tokens": 32000})
    pdf = make_pdf(tmp_path / "tokens sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    client.post(f"/api/documents/{doc_id}/transcribe", json={"pages": "1"})
    assert seen["name"] == "openai" and seen["max_tokens"] == 32000
