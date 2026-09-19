"""Web UI API tests with FastAPI's TestClient (no browser, no network)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from unscanner import webapp
from unscanner.sanitize import sanitize_fragment
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
    # ...and on table cells (a <th> is centered by browsers, so Left is an explicit align-left there)
    assert sanitize_fragment('<table><tr><th scope="row" class="align-left big">a</th><td class="align-right" style="x">1</td></tr></table>') ==         '<table><tr><th scope="row" class="align-left">a</th><td class="align-right">1</td></tr></table>'
    # a numbered list continued from the previous page keeps its start; junk values do not survive
    assert sanitize_fragment('<ol start="4" type="a" reversed><li>x</li></ol>') == '<ol start="4" type="a"><li>x</li></ol>'
    assert sanitize_fragment('<ol start="iv" type="disc"><li>x</li></ol><ul start="2"><li>y</li></ul>') ==         '<ol><li>x</li></ol><ul><li>y</li></ul>'
    # small caps survive on a span only, and alignment classes do not ride along on one
    assert sanitize_fragment('<p><span class="small-caps align-center" style="x">John Barner</span> was</p>') ==         '<p><span class="small-caps">John Barner</span> was</p>'
    assert sanitize_fragment('<p class="small-caps align-right">x</p>') == '<p class="align-right">x</p>'


def test_sanitize_drops_source_view_indentation():
    flat = ('<ul><li>one <em>a</em> <strong>b</strong></li><li>two<ul><li>y</li></ul></li></ul>'
            '<figure><img src="fig:1-1" alt="x"><figcaption>cap</figcaption></figure>'
            '<table><tbody><tr><th scope="col">h</th><td>d</td></tr></tbody></table><pre>a\n  b</pre>')
    pretty = ('<ul>\n  <li>one <em>a</em> <strong>b</strong></li>\n  <li>\n    two\n    <ul>\n      <li>y</li>\n    </ul>\n  </li>\n</ul>\n'
              '<figure>\n  <img src="fig:1-1" alt="x">\n  <figcaption>cap</figcaption>\n</figure>\n'
              '<table>\n  <tbody>\n    <tr>\n      <th scope="col">h</th>\n      <td>d</td>\n    </tr>\n  </tbody>\n</table>\n'
              '<pre>a\n  b</pre>')
    assert sanitize_fragment(pretty) == sanitize_fragment(flat) == flat


def test_open_edit_build_validate(client, tmp_path):
    pdf = make_pdf(tmp_path / "ui sample.pdf")
    r = client.post("/api/documents", json={"pdf_path": str(pdf), "title": "UI Sample"})
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]
    assert r.json()["page_count"] == 3

    assert client.get("/").status_code == 200 and "Unscanner" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    # the toolbar's symbol dropdown offers the fractions OCR tends to confuse, and app.js handles it
    assert 'id="symbol-picker"' in client.get("/").text and "<option>¼</option>" in client.get("/").text
    assert "#symbol-picker" in client.get("/static/app.js").text
    # F2 is Approve & next, and the button says so to sighted and screen reader users
    assert 'aria-keyshortcuts="F2"' in client.get("/").text
    assert 'e.key === "F2"' in client.get("/static/app.js").text
    # arrow keys get the caret out of a table that ends or starts the page
    assert "function escapeTable(e)" in client.get("/static/app.js").text
    # the UI code is revalidated on every load, so a restart picks up changes to it
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"

    r = client.get(f"/api/documents/{doc_id}/pages/1")
    assert "CHAPTER ONE" in r.json()["draft_text"]
    img = client.get(f"/api/documents/{doc_id}/pages/1/image")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    assert img.headers["cache-control"] == "no-cache"  # a page's scan can change under the same URL

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


def test_figure_rotation_is_previewed_stored_and_built(client, tmp_path):
    import io

    from PIL import Image

    def size(png: bytes) -> tuple[int, int]:
        return Image.open(io.BytesIO(png)).size

    pdf = make_pdf(tmp_path / "rot sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    html = '<figure><img src="fig:1-1" alt="A wide box."></figure><p>Text here for the page.</p>'
    fig = {"id": "1-1", "alt": "A wide box.", "bbox": [100, 100, 500, 200], "caption": ""}
    client.put(f"/api/documents/{doc_id}/pages/1", json={"html": html, "figures": [fig]})
    url = f"/api/documents/{doc_id}/pages/1/figure/1-1"
    w, h = size(client.get(url).content)
    assert w > h
    # the live preview turns an unsaved crop; a save stores the rotation (normalized to a quarter turn)
    assert size(client.get(url, params={"bbox": "100,100,500,200", "rotate": 90}).content) == (h, w)
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={"html": html, "version": 1,
                                                             "figures": [{**fig, "rotate": -90}]})
    assert r.status_code == 200
    assert client.get(f"/api/documents/{doc_id}/pages/1").json()["figures"][0]["rotate"] == 270
    assert size(client.get(url).content) == (h, w)
    client.post(f"/api/documents/{doc_id}/build")
    built = client.get(f"/api/documents/{doc_id}/figures/p001-1-1.png")
    assert size(built.content) == (h, w)


def test_figure_alt_autofill_and_layout_classes(client, tmp_path, monkeypatch):
    class DescribingBackend(FakeBackend):
        def describe_image(self, image_png, prompt):
            assert image_png[:4] == b"\x89PNG" and "Caption printed with the image: Figure 1" in prompt
            return "A grey rectangle standing in for a chart."

    monkeypatch.setattr("unscanner.backends.make_backend", lambda *a, **k: DescribingBackend())
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


def test_region_is_reread_as_a_table(client, tmp_path, monkeypatch):
    seen = {}

    class TableBackend(FakeBackend):
        reply = ('Here is the table:\n```html\n<table class="x" style="y"><tr><th scope="row">General military purposes</th>'
                 '<td>$1,000</td></tr><tr><td onclick="z()">Navy</td><td></td></tr></table>\n```')

        def describe_image(self, image_png, prompt, max_tokens=None):
            seen.update(png=image_png, prompt=prompt, max_tokens=max_tokens)
            return self.reply

    monkeypatch.setattr("unscanner.backends.make_backend", lambda *a, **k: TableBackend())
    pdf = make_pdf(tmp_path / "table sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    url = f"/api/documents/{doc_id}/pages/1/table"
    # the editor's selected text is the wording hint; the model sees only the crop, with a table-sized budget
    r = client.post(url, json={"bbox": [50, 50, 950, 500], "text": "General military purposes $1,000"})
    assert r.status_code == 200, r.text
    assert seen["png"][:4] == b"\x89PNG" and seen["max_tokens"] == webapp.TABLE_MAX_TOKENS
    assert "General military purposes $1,000" in seen["prompt"] and "<table>" in seen["prompt"]
    # fence and prose dropped, and the table sanitized like everything else that reaches the editor
    assert r.json()["html"] == ('<table><tr><th scope="row">General military purposes</th><td>$1,000</td></tr>'
                                '<tr><td>Navy</td><td></td></tr></table>')
    # nothing is stored: the editor inserts the table and the person saves
    assert client.get(f"/api/documents/{doc_id}/pages/1").json()["version"] == 0
    # without selected text, the scan's own words inside the box are the hint
    client.post(url, json={"bbox": [0, 0, 1000, 1000]})
    assert "CHAPTER ONE" in seen["prompt"]
    assert client.post(url, json={"bbox": [500, 0, 100, 1000]}).status_code == 400
    assert client.post(f"/api/documents/{doc_id}/pages/99/table", json={"bbox": [0, 0, 9, 9]}).status_code == 404
    TableBackend.reply = "I see no table here."
    r = client.post(url, json={"bbox": [0, 0, 1000, 1000]})
    assert r.status_code == 502 and "no <table>" in r.json()["detail"]


def test_settings_roundtrip(client):
    # untouched settings pre-fill the model the guidelines were tuned against
    assert client.get("/api/settings").json()["openai_model"] == "qwen3.6:27b"
    r = client.put("/api/settings", json={"backend": "openai", "openai_model": "qwen2.5vl:7b", "junk": 1})
    assert r.json()["backend"] == "openai" and "junk" not in r.json()
    assert client.get("/api/settings").json()["openai_model"] == "qwen2.5vl:7b"


def test_transcribe_job(client, tmp_path, monkeypatch):
    monkeypatch.setattr("unscanner.backends.make_backend", lambda *a, **k: FakeBackend())
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
    import unscanner.backends as backends

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


def test_document_properties(client, tmp_path):
    pdf = make_pdf(tmp_path / "props.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf), "title": "Old"}).json()["doc_id"]
    r = client.put(f"/api/documents/{doc_id}/properties",
                   json={"title": " New Title ", "author": "A. Writer", "language": "fr-CA"})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/documents/{doc_id}").json()
    assert (d["title"], d["author"], d["language"]) == ("New Title", "A. Writer", "fr-CA")
    assert client.put(f"/api/documents/{doc_id}/properties", json={"title": "  ", "language": "en"}).status_code == 400
    assert client.put(f"/api/documents/{doc_id}/properties", json={"title": "T", "language": "english!"}).status_code == 400
    # the build uses them
    html = client.post(f"/api/documents/{doc_id}/build").json()["html"]
    from pathlib import Path
    out = Path(html).read_text(encoding="utf-8")
    assert '<html lang="fr-CA">' in out and "<title>New Title</title>" in out


def test_page_diff_reports_dropped_and_invented_words(client, tmp_path):
    pdf = make_pdf(tmp_path / "diff sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf), "title": "Diff"}).json()["doc_id"]
    # page 2 of the synthetic PDF: "38 / very long indeed. Here is a second paragraph. / A Section / ..."
    words = "very long indeed. Here is a wholly invented paragraph. A Section Section text with a note.1".split()
    r = client.post(f"/api/documents/{doc_id}/pages/2/diff", json={"words": words})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["text"] for t in d["scan"] if t["kind"] == "missing"] == ["38", "second"]
    assert [words[e["index"]] for e in d["editor"]] == ["wholly", "invented"]
    assert all(0 <= b["x0"] < b["x1"] <= 1000 for t in d["scan"] for b in t["boxes"])
    assert client.post(f"/api/documents/{doc_id}/pages/99/diff", json={"words": []}).status_code == 404
