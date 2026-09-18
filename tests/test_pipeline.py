"""End-to-end tests with a fake backend (no network): open PDF -> transcribe -> build -> validate -> MCP."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pymupdf
import pytest

from remediate.assemble import assemble
from remediate.backends.base import Backend
from remediate.document import Document, parse_page_range
from remediate.epub import build_epub
from remediate.pdf import new_document
from remediate.pipeline import transcribe_pages
from remediate.prompts import normalize_result, parse_model_json
from remediate.validate import coverage, validate_html


def make_pdf(path: Path, n_pages: int = 3) -> Path:
    doc = pymupdf.open()
    texts = [
        "CHAPTER ONE\nThe Beginning\nThis is the first paragraph of the chapter. It continues onto the next page because it is",
        "38\nvery long indeed. Here is a second paragraph.\nA Section\nSection text with a note.1",
        "39\n1. The footnote text.",
    ]
    for t in texts:
        page = doc.new_page(width=400, height=600)
        page.insert_text((40, 60), t, fontsize=11)
    doc.save(str(path))
    return path


class FakeBackend(Backend):
    name = "fake"
    model = "fake-1"
    results = {
        1: {"label": None, "skip": False, "starts_mid_paragraph": False, "ends_mid_paragraph": True,
            "html": "<h1>Chapter One: The Beginning</h1><p>This is the first paragraph of the chapter. It "
                    "continues onto the next page because it is</p>", "figures": [], "notes": ""},
        2: {"label": "38", "skip": False, "starts_mid_paragraph": True, "ends_mid_paragraph": False,
            "html": "<p>very long indeed. Here is a second paragraph.</p><h2>A Section</h2>"
                    "<p>Section text with a note.<sup><a href=\"#fn-38-1\" id=\"fnref-38-1\" role=\"doc-noteref\">1</a></sup></p>"
                    "<figure><img src=\"fig:38-1\" alt=\"A plain grey rectangle used as a placeholder figure.\">"
                    "<figcaption>Figure 1</figcaption></figure>",
            "figures": [{"id": "38-1", "alt": "A plain grey rectangle.", "bbox": [100, 100, 400, 300], "caption": "Figure 1"}],
            "notes": ""},
        3: {"label": "39", "skip": False, "starts_mid_paragraph": False, "ends_mid_paragraph": False,
            "html": "<aside role=\"doc-footnote\" id=\"fn-38-1\"><p><a href=\"#fnref-38-1\" role=\"doc-backlink\">1.</a> "
                    "The footnote text.</p></aside>", "figures": [], "notes": "footnote page"},
    }

    def transcribe(self, image_png: bytes, user_prompt: str):
        idx = int(user_prompt.split("This is PDF page ")[1].split(" ")[0])
        return normalize_result(self.results[idx]), {"input_tokens": 10, "output_tokens": 5}


@pytest.fixture
def doc(tmp_path):
    pdf = make_pdf(tmp_path / "sample book.pdf")
    return new_document(pdf, tmp_path / "work", title="Sample Book", author="A. Author")


def test_open_and_state(doc, tmp_path):
    assert len(doc.pages) == 3
    assert doc.title == "Sample Book"
    again = Document.load(doc.workdir)
    assert again.pages[0].status == "pending"
    assert parse_page_range("1-2,3", 3) == [1, 2, 3]
    assert parse_page_range("2", 3) == [2]


def test_transcribe_build_validate(doc, tmp_path):
    summary = transcribe_pages(doc, FakeBackend(), [1, 2, 3], workers=2)
    assert summary["done"] == 3 and summary["errors"] == 0
    doc = Document.load(doc.workdir)
    assert doc.page(2).label == "38"
    assert doc.page(1).draft_source == "textlayer"
    assert doc.page(3).status == "needs_review"

    out = tmp_path / "out"
    a = assemble(doc, out)
    html = a.html()
    # paragraph split across pages is joined with an inline page marker
    assert "because it is <span role=\"doc-pagebreak\" id=\"pg-38\"" in html
    assert "very long indeed" in html
    assert html.count("<h1") == 1
    assert 'role="doc-pagebreak"' in html
    assert "figures/p002-38-1.png" in html and (out / "figures" / "p002-38-1.png").exists()
    issues = validate_html(html)
    assert not [i for i in issues if i.severity == "error"], [i.message for i in issues]
    cov = coverage(doc)
    assert not [i for i in cov if i.severity == "error"]

    epub = build_epub(a, out / "book.epub", author=doc.author, source="sample book.pdf")
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        opf = z.read("OEBPS/package.opf").decode()
        nav = z.read("OEBPS/nav.xhtml").decode()
        assert "schema:accessibilityFeature" in opf and "printPageNumbers" in opf
        assert 'epub:type="page-list"' in nav and "#pg-38" in nav
        assert any(n.startswith("OEBPS/text/ch") for n in names)
        # every xhtml is well-formed XML and every image it references exists at the resolved path
        import posixpath

        from lxml import etree

        for n in names:
            if n.endswith(".xhtml"):
                root = etree.fromstring(z.read(n))
                for im in root.iter("{http://www.w3.org/1999/xhtml}img"):
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(n), im.get("src")))
                    assert target in names, f"{n} references missing {target}"


def test_parse_model_json_tolerates_fences():
    d = parse_model_json("```json\n{\"html\": \"<p>x</p>\", \"label\": 3}\n```")
    assert normalize_result(d)["label"] == "3"
    d = parse_model_json("Here you go: {\"html\": \"<p>y</p>\"} thanks")
    assert d["html"] == "<p>y</p>"


def test_figure_id_drops_fig_prefix():
    """A model that repeats the src prefix in the figure id must still match <img src="fig:ID">."""
    from remediate.document import Figure, Page
    assert Figure(id="fig:page-951-1", alt="x").id == "page-951-1"
    assert Page.from_dict({"index": 1, "figures": [{"id": "fig:1-1", "alt": ""}]}).figures[0].id == "1-1"
    assert Figure(id="1-1", alt="").id == "1-1"


def test_heading_normalization(tmp_path, doc):
    doc.page(1).html = "<h1>Part A</h1><p>a</p>"
    doc.page(2).html = "<h1>Part B</h1><h4>Deep</h4><p>b</p>"
    doc.page(3).html = "<p>c</p>"
    for p in doc.pages:
        p.status = "done"
    a = assemble(doc, tmp_path / "out")
    html = a.html()
    assert html.count("<h1") == 1 and "<h1 id=\"h-1\">Sample Book</h1>" in html
    assert "<h2" in html and "<h3" in html and "<h4" not in html  # demoted then skip closed


def test_image_without_crop_becomes_description(tmp_path, doc):
    doc.page(1).html = "<h1>T</h1><figure><img src=\"fig:1-1\" alt=\"A map of the region.\"></figure><p>a</p>"
    doc.page(2).html = "<p>b</p><img src=\"fig:2-1\">"
    doc.page(3).html = "<p>c</p>"
    for p in doc.pages:
        p.status = "done"
    a = assemble(doc, tmp_path / "out")
    html = a.html()
    assert "<img" not in html
    assert '<p class="figure-description"><strong>Image: </strong>A map of the region.</p>' in html
    assert "(no description)" in html
    assert any("image without description" in w for w in a.warnings)


@pytest.mark.anyio
async def test_mcp_server_roundtrip(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "mcp sample.pdf")
    monkeypatch.setenv("REMEDIATE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("REMEDIATE_OUT_DIR", str(tmp_path / "out"))
    from mcp.client.client import Client

    from remediate.mcp_server import server

    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"open_document", "get_page", "set_page", "build", "validate", "get_guidelines"} <= tools
        r = await client.call_tool("open_document", {"pdf_path": str(pdf), "title": "MCP Sample"})
        doc_id = r.structured_content["doc_id"]
        r = await client.call_tool("get_page", {"doc_id": doc_id, "page": 1})
        kinds = [c.type for c in r.content]
        assert "image" in kinds and "text" in kinds
        info = json.loads(next(c.text for c in r.content if c.type == "text"))
        assert "CHAPTER ONE" in info["draft_text"]
        for i, html in [(1, "<h1>Chapter One</h1><p>First page text that is long enough to count.</p>"),
                        (2, "<p>Second page text with several words in it for coverage.</p>"),
                        (3, "<p>Third page text.</p>")]:
            r = await client.call_tool("set_page", {"doc_id": doc_id, "page": i, "html": html, "label": str(36 + i)})
            assert r.structured_content["status"] == "done"
        r = await client.call_tool("build", {"doc_id": doc_id})
        assert Path(r.structured_content["html"]).exists() and Path(r.structured_content["epub"]).exists()
        r = await client.call_tool("validate", {"doc_id": doc_id, "epubcheck": False})
        errors = [i for i in r.structured_content["issues"] if i["severity"] == "error"]
        assert not errors, errors


def test_page_load_failure_is_a_page_error(doc, monkeypatch):
    import remediate.pipeline as pl

    real = pl.cached_page_png

    def boom(d, i):
        if i == 2:
            raise RuntimeError("render failed")
        return real(d, i)

    monkeypatch.setattr(pl, "cached_page_png", boom)
    summary = transcribe_pages(doc, FakeBackend(), [1, 2, 3], workers=2)
    assert summary["done"] == 2 and summary["errors"] == 1
    assert doc.page(2).status == "error" and "render failed" in doc.page(2).notes
    assert doc.page(1).status in ("done", "needs_review") and doc.page(3).status in ("done", "needs_review")


def test_store_failure_marks_the_page_not_the_run(doc, monkeypatch):
    import remediate.pipeline as pl

    real = pl.apply_result

    def bad(page, result, model="", usage=None, changed_by=None):
        if page.index == 2:
            raise ValueError("bad figure")
        return real(page, result, model, usage, changed_by)

    monkeypatch.setattr(pl, "apply_result", bad)
    summary = transcribe_pages(doc, FakeBackend(), [1, 2, 3], workers=2)
    assert summary["done"] == 2 and summary["errors"] == 1
    assert doc.page(2).status == "error" and "bad figure" in doc.page(2).notes


def test_fatal_error_cancels_the_queue(doc, monkeypatch):
    """If the state file cannot be written the run must stop, not transcribe every page into the void."""
    import time as _time
    import remediate.pipeline as pl

    pl.ensure_draft_text(doc, [1, 2, 3])
    calls = []

    class Slow(FakeBackend):
        def transcribe(self, png, prompt):
            calls.append(prompt)
            _time.sleep(0.05)
            return super().transcribe(png, prompt)

    def bad_save():
        raise PermissionError("doc.json is locked")

    monkeypatch.setattr(doc, "save", bad_save)
    with pytest.raises(PermissionError):
        transcribe_pages(doc, Slow(), [1, 2, 3], workers=1)
    assert len(calls) < 3
