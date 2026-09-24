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


def test_favicon_is_linked_and_served(client):
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in client.get("/").text
    for url in ("/static/favicon.svg", "/favicon.ico"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
        assert r.text.lstrip().startswith("<svg")


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
    # the File menu holds Open PDF, Save, Recent projects, Manage projects, Export, Import and Settings;
    # Open PDF and Save stay on the toolbar too, and the old document dropdown is gone
    html, js = client.get("/").text, client.get("/static/app.js").text
    assert 'id="btn-file"' in html and 'aria-haspopup="menu"' in html and 'id="file-menu" class="app-menu" role="menu"' in html
    for item in ("menu-open", "menu-save", "menu-recent", "recent-menu", "btn-documents", "btn-export", "btn-import", "btn-settings"):
        assert f'id="{item}"' in html, item
    assert 'id="btn-open"' in html and 'id="btn-save-all"' in html
    assert "doc-select" not in html and "doc-select" not in js
    assert "function renderRecent()" in js
    # the page list's hidden status labels stay in their row, so a long document does not stretch the page
    assert ".page-list li { position: relative;" in client.get("/static/style.css").text
    # File > Help opens a dialog explaining the features in plain words
    assert 'id="btn-help" type="button" role="menuitem"' in html and 'id="dlg-help"' in html
    assert '$("#btn-help").addEventListener' in js
    for topic in ("help-steps", "help-pages", "help-figures", "help-keys"):
        assert f'href="#{topic}"' in html and f'id="{topic}"' in html, topic
    # a project is the work on a PDF; the menu says "project" so the two are not confused
    assert ">Recent projects <" in html and ">Manage projects…</button>" in html and "Recent documents" not in html
    # the open document's title is in the title bar, not on the page
    assert "doc-title" not in html and "function setWindowTitle(title)" in js
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

    # Export settings: per format, applied by the next build
    assert 'name="epub_page_numbers"' in client.get("/").text and '"page_numbers"' in client.get("/static/app.js").text
    s = client.get("/api/settings").json()
    assert (s["html_indent"], s["epub_indent"], s["html_page_numbers"], s["epub_justify"]) == (False, True, True, False)
    assert s["html_bookmarks"] is True
    assert 'name="html_bookmarks"' in client.get("/").text and '"html_bookmarks"' in client.get("/static/app.js").text
    client.put("/api/settings", json={"html_indent": True, "html_page_numbers": False, "epub_indent": False,
                                      "html_bookmarks": True})
    client.put(f"/api/documents/{doc_id}/pages/3", json={"html": "<h2>Third</h2><p>Third.</p>", "label": "39"})
    assert client.post(f"/api/documents/{doc_id}/build").status_code == 200
    built = client.get(f"/api/documents/{doc_id}/preview").text
    assert "text-indent" in built and 'id="pg-' not in built
    # the heading outline, at the top of the HTML, each heading a link to its place in the text
    assert '<summary>Bookmarks</summary>' in built and '<a href="#h-2">Third</a>' in built
    codes = [i["code"] for i in client.get(f"/api/documents/{doc_id}/validate", params={"epubcheck": "false"}).json()["issues"]]
    assert "no-pagebreaks" not in codes  # left out on purpose, so not reported
    import zipfile
    with zipfile.ZipFile(sorted((tmp_path / "out").glob("*/*.epub"))[-1]) as z:
        assert "text-indent" not in z.read("OEBPS/style.css").decode()
        assert "page-list" in z.read("OEBPS/nav.xhtml").decode()


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


def test_bulk_skip_approve_and_flag(client, tmp_path):
    pdf = make_pdf(tmp_path / "bulk sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    html = '<figure><img src="fig-1" alt="A"></figure><p>Text</p>'
    client.put(f"/api/documents/{doc_id}/pages/1", json={"html": html, "label": "37", "notes": "check", "status": "needs_review",
                                                         "figures": [{"id": "fig-1", "alt": "A", "bbox": [0.1, 0.1, 0.5, 0.5]}]})
    bulk = lambda pages, action: client.post(f"/api/documents/{doc_id}/pages/bulk", json={"pages": pages, "action": action})

    # Approve: the transcribed page is approved, the empty one stays "not transcribed".
    by_index = {p["index"]: p for p in bulk([1, 2], "approve").json()["pages"]}
    assert (by_index[1]["status"], by_index[2]["status"]) == ("done", "pending")
    # What the page list's "Has figures" / "Has tables" filters go by.
    assert (by_index[1]["has_figures"], by_index[1]["has_tables"], by_index[2]["has_figures"]) == (True, False, False)
    client.put(f"/api/documents/{doc_id}/pages/2", json={"html": "<table><tr><td>1</td></tr></table>"})
    listed = client.get(f"/api/documents/{doc_id}").json()["pages"]
    assert (listed[1]["has_tables"], listed[1]["has_figures"]) == (True, False)
    client.put(f"/api/documents/{doc_id}/pages/2", json={"html": ""})
    # The content is untouched, and the version moved on so an editor holding version 1 gets a 409.
    page = client.get(f"/api/documents/{doc_id}/pages/1").json()
    assert (page["html"], page["label"], page["notes"], page["version"]) == (sanitize_fragment(html), "37", "check", 2)
    assert page["figures"][0]["bbox"] == [0.1, 0.1, 0.5, 0.5]
    assert client.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>x</p>", "version": 1}).status_code == 409

    assert [p["status"] for p in bulk([1], "needs_review").json()["pages"]] == ["needs_review"]

    # Skip and un-skip never touch the status: skipping pages for one build must not change what is approved.
    assert [(p["skip"], p["status"]) for p in bulk([1, 2], "skip").json()["pages"]] == [(True, "needs_review"), (True, "pending")]
    assert [(p["skip"], p["status"]) for p in bulk([1, 2], "unskip").json()["pages"]] == [(False, "needs_review"), (False, "pending")]
    bulk([1], "approve")
    assert [(p["skip"], p["status"]) for p in bulk([1], "skip").json()["pages"]] == [(True, "done")]
    assert [(p["skip"], p["status"]) for p in bulk([1], "unskip").json()["pages"]] == [(False, "done")]

    assert bulk([1], "delete").status_code == 400
    assert bulk([99], "skip").status_code == 404


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
    r = client.post(f"/api/documents/{doc_id}/build")
    assert r.json()["figures"] == 1
    # ...and the build crops it; the HTML holds the image itself unless that export setting is off.
    assert client.get("/api/settings").json()["html_embed_images"] is True
    assert 'name="html_embed_images"' in client.get("/").text and "html_embed_images" in client.get("/static/app.js").text
    assert 'src="data:image/png;base64,' in client.get(f"/api/documents/{doc_id}/preview").text
    client.put("/api/settings", json={"html_embed_images": False})
    client.post(f"/api/documents/{doc_id}/build")
    assert 'src="figures/p001-1-1.png"' in client.get(f"/api/documents/{doc_id}/preview").text
    # Smaller images: the build writes grayscale JPEGs and the figure route serves them as such.
    s = client.get("/api/settings").json()
    assert (s["images_grayscale"], s["images_jpeg"]) == (False, False)
    assert 'name="images_jpeg"' in client.get("/").text and '"images_grayscale"' in client.get("/static/app.js").text
    client.put("/api/settings", json={"images_grayscale": True, "images_jpeg": True})
    client.post(f"/api/documents/{doc_id}/build")
    assert 'src="figures/p001-1-1.jpg"' in client.get(f"/api/documents/{doc_id}/preview").text
    jpg = client.get(f"/api/documents/{doc_id}/figures/p001-1-1.jpg")
    assert jpg.headers["content-type"] == "image/jpeg" and jpg.content[:2] == bytes([0xFF, 0xD8])
    assert client.get(f"/api/documents/{doc_id}/figures/p001-1-1.png").status_code == 404
    client.put("/api/settings", json={"images_grayscale": False, "images_jpeg": False})
    client.post(f"/api/documents/{doc_id}/build")


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
    # the file is named after the title, and a build leaves no file from a former title behind
    assert Path(html).name == "new-title.html"
    r = client.get(f"/api/documents/{doc_id}/output/html")
    assert r.status_code == 200 and "new-title.html" in r.headers["content-disposition"]
    client.put(f"/api/documents/{doc_id}/properties", json={"title": "Newer", "language": "en"})
    html = client.post(f"/api/documents/{doc_id}/build").json()["html"]
    assert [f.name for f in Path(html).parent.glob("*.html")] == ["newer.html"]
    assert "<title>Newer</title>" in client.get(f"/api/documents/{doc_id}/preview").text


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


def test_outline_lists_headings_for_the_headings_sidebar(client, tmp_path):
    pdf = make_pdf(tmp_path / "outline sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf), "title": "Outline"}).json()["doc_id"]
    client.put(f"/api/documents/{doc_id}/pages/1", json={
        "html": "<h1>Chapter\n  One</h1><p>Text.</p><h2>A <em>first</em> part</h2><p>More.</p>", "label": "7"})
    # a heading printed over two lines: the break has to read as a space, not run the lines together
    client.put(f"/api/documents/{doc_id}/pages/2", json={
        "html": "<h3>Chapter IX.<br>The Civil War</h3><p>Text.</p>", "label": "8"})
    # a skipped page is left out of the output, so it is left out of the outline too
    client.put(f"/api/documents/{doc_id}/pages/3", json={"html": "<h2>Front matter</h2>", "skip": True})
    heads = client.get(f"/api/documents/{doc_id}/outline").json()["headings"]
    assert [(h["level"], h["text"], h["page"], h["label"], h["nth"]) for h in heads] == [
        (1, "Chapter One", 1, "7", 0),
        (2, "A first part", 1, "7", 1),
        (3, "Chapter IX. The Civil War", 2, "8", 0),
    ]
    # levels are as written, not the normalized ones the build produces
    client.put(f"/api/documents/{doc_id}/pages/2", json={"html": "<h5>Deeper</h5>", "label": "8", "version": 1})
    assert client.get(f"/api/documents/{doc_id}/outline").json()["headings"][2]["level"] == 5
    assert client.get("/api/documents/nope/outline").status_code == 404


def test_window_title_follows_the_open_document(client, monkeypatch):
    from unscanner import desktop

    # in a browser tab there is no window: the page sets its own document.title, the server does nothing
    r = client.put("/api/app/title", json={"title": "Sumter Slave Bounty"})
    assert r.json() == {"title": "Sumter Slave Bounty - Unscanner", "window": False}

    class FakeWindow:
        titles: list[str] = []

        def set_title(self, title):
            self.titles.append(title)

    monkeypatch.setattr(desktop, "window", FakeWindow())
    assert client.put("/api/app/title", json={"title": " Sumter Slave Bounty "}).json()["window"] is True
    assert client.put("/api/app/title", json={"title": ""}).json()["title"] == "Unscanner"
    assert FakeWindow.titles == ["Sumter Slave Bounty - Unscanner", "Unscanner"]
