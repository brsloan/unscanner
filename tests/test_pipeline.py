"""End-to-end tests with a fake backend (no network): open PDF -> transcribe -> build -> validate -> MCP."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pymupdf
import pytest
from lxml import html as lhtml

from unscanner.assemble import assemble
from unscanner.backends.base import Backend
from unscanner.document import Document, parse_page_range
from unscanner.epub import build_epub
from unscanner.pdf import new_document
from unscanner.pipeline import transcribe_pages
from unscanner.prompts import normalize_result, parse_model_json
from unscanner.validate import coverage, validate_html


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

    def transcribe(self, image_png: bytes, user_prompt: str, system=None):
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


def test_document_follows_a_renamed_project_folder(tmp_path):
    old = tmp_path / "old-name"
    (old / "pdfs").mkdir(parents=True)
    pdf = make_pdf(old / "pdfs" / "sample book.pdf")
    doc = new_document(pdf, old / "work", title="Sample Book")
    name = Path(doc.workdir).name
    new = tmp_path / "new-name"
    old.rename(new)

    moved = Document.load(new / "work" / name)
    assert Path(moved.workdir) == (new / "work" / name).resolve()
    assert Path(moved.source) == (new / "pdfs" / "sample book.pdf").resolve()
    moved.save()
    assert not old.exists()  # saving must not recreate the old folder
    assert json.loads((new / "work" / name / "doc.json").read_text(encoding="utf-8"))["workdir"] == moved.workdir


def test_document_keeps_a_pdf_that_did_not_move(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    pdf = make_pdf(tmp_path / "elsewhere" / "sample book.pdf")
    doc = new_document(pdf, tmp_path / "a" / "work")
    name = Path(doc.workdir).name
    (tmp_path / "a").rename(tmp_path / "b")
    assert Document.load(tmp_path / "b" / "work" / name).source == doc.source


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

    # Images inside the file (the HTML export default): one self-contained file, the same bytes as the crop
    import base64
    from unscanner.assemble import ExportStyle, export_style
    assert export_style("html").embed_images and not export_style("epub").embed_images
    assert not export_style("html", {"html_embed_images": False}).embed_images
    single = a.html(ExportStyle(embed_images=True))
    crop = base64.b64encode((out / "figures" / "p002-38-1.png").read_bytes()).decode()
    assert f'src="data:image/png;base64,{crop}"' in single and 'src="figures/' not in single
    assert "figures/p002-38-1.png" in a.html()  # the assembled document itself is untouched
    issues = validate_html(single)
    assert not [i for i in issues if i.severity == "error"], [i.message for i in issues]
    assert all(len(i.location) < 200 for i in issues)  # never the base64 as a location

    # Smaller images: grayscale and/or JPEG figure files, used by the HTML and the EPUB alike
    import io
    from PIL import Image
    from unscanner.assemble import ImageOptions, image_options
    assert image_options() == ImageOptions(False, False)
    assert image_options({"images_grayscale": True, "images_jpeg": True}) == ImageOptions(True, True)
    out2 = tmp_path / "out2"  # its own folder: a JPEG build clears the PNG the build above still uses
    grey = assemble(doc, out2, ImageOptions(grayscale=True))
    assert Image.open(grey.figures["p002-38-1.png"]).mode == "L"
    small = assemble(doc, out2, ImageOptions(grayscale=True, jpeg=True))
    jpg = out2 / "figures" / "p002-38-1.jpg"
    assert list(small.figures) == ["p002-38-1.jpg"] and not (out2 / "figures" / "p002-38-1.png").exists()
    im = Image.open(jpg)
    assert (im.format, im.mode) == ("JPEG", "L")
    assert Image.open(io.BytesIO(crop_bytes := base64.b64decode(crop))).size == im.size and crop_bytes
    single = small.html(ExportStyle(embed_images=True))
    assert 'src="data:image/jpeg;base64,' + base64.b64encode(jpg.read_bytes()).decode() in single
    epub = build_epub(small, tmp_path / "small.epub")
    with zipfile.ZipFile(epub) as z:
        assert "OEBPS/figures/p002-38-1.jpg" in z.namelist()
        assert 'href="figures/p002-38-1.jpg" media-type="image/jpeg"' in z.read("OEBPS/package.opf").decode()
    assert Image.open(assemble(doc, out2, ImageOptions(jpeg=True)).figures["p002-38-1.jpg"]).mode == "RGB"
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
        # default export settings: book paragraphs in the EPUB only, nothing justified
        css = z.read("OEBPS/style.css").decode()
        assert "p { margin: 0; text-indent: 0; }" in css and "p + p," in css
        assert "text-indent" not in html and "justify" not in html + css
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


def test_batch_html_is_sanitized(doc):
    """Batch results go through the sanitizer like editor and MCP edits (no stray attributes or styles)."""
    class MessyBackend(FakeBackend):
        results = {1: {"html": '<h1 style="color:red">T</h1><figure><img src="fig:1-1" alt="A map." '
                               'bbox="[100, 180, 850, 650]"></figure><p class="junk" onclick="x()">Body.</p>',
                       "figures": [{"id": "1-1", "alt": "A map.", "bbox": [100, 180, 850, 650], "caption": ""}]}}

    summary = transcribe_pages(doc, MessyBackend(), [1])
    assert summary["done"] == 1
    html = Document.load(doc.workdir).page(1).html
    for bad in ("bbox=", "style=", "onclick", "junk"):
        assert bad not in html
    assert 'src="fig:1-1"' in html and 'alt="A map."' in html and "<h1>T</h1>" in html


def test_figure_id_drops_fig_prefix():
    """A model that repeats the src prefix in the figure id must still match <img src="fig:ID">."""
    from unscanner.document import Figure, Page
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


def test_knit_word():
    from unscanner.assemble import knit_word

    assert knit_word("the inter-", "national order") == "the inter"
    assert knit_word("a full sentence.", "Next") is None and knit_word("a dash -", "then") is None
    # the document's own spelling decides whether the hyphen belongs to the word
    assert knit_word("of self-", "government", {"self-government"}) == "of self-"
    assert knit_word("of self-", "government", {"self-government", "selfgovernment"}) == "of self"
    assert knit_word("her mother-in-", "law") == "her mother-in-"
    assert knit_word("the Anglo-", "Saxon") == "the Anglo-" and knit_word("1914-", "1918") == "1914-"
    assert knit_word("soft­", "ened") == "soft"


def test_html_bookmarks_are_a_nested_collapsible_outline(tmp_path, doc):
    from unscanner.assemble import ExportStyle, export_style
    from unscanner.validate import validate_html

    assert export_style("html").bookmarks  # on unless the Export settings turn it off
    assert not export_style("html", {"html_bookmarks": False}).bookmarks
    assert not export_style("epub", {"html_bookmarks": True}).bookmarks  # an EPUB has its own TOC

    doc.page(1).html = "<h1>Sample Book</h1><h2>Chapter One</h2><p>Text.</p><h3>A part<br>of it</h3><p>More.</p>"
    doc.page(2).html = "<h2>Chapter Two</h2><p>Text.</p>"
    doc.page(3).html = "<p>Text.</p>"
    for n, p in enumerate(doc.pages):
        p.status, p.label = "done", str(10 + n)
    a = assemble(doc, tmp_path / "out")
    assert "Bookmarks" not in a.html()  # the switch off: no nav, whatever the settings default is

    head = a.html(ExportStyle(bookmarks=True)).split("</style>")[1]
    assert '<nav class="bookmarks" aria-label="Bookmarks"><details><summary>Bookmarks</summary>' in head
    # the h1 is the title, already at the top of the page; a chapter with sub-headings folds away
    assert "<summary>Sample Book" not in head
    assert '<li><details open><summary><a href="#h-2">Chapter One</a></summary>' in head
    assert '<li class="leaf"><a href="#h-3">A part of it</a></li></ul></details></li>' in head
    assert '<li class="leaf"><a href="#h-4">Chapter Two</a></li>' in head
    assert head.index("Bookmarks") < head.index("<main")  # at the beginning, before the text
    assert [i.code for i in validate_html(a.html(ExportStyle(bookmarks=True)))] == []

    # nothing to list but the title: no bookmarks at all
    doc.page(1).html = "<h1>Sample Book</h1><p>Text.</p>"
    doc.page(2).html = "<p>Text.</p>"
    plain = assemble(doc, tmp_path / "out2").html(ExportStyle(bookmarks=True))
    assert "bookmarks" not in plain.split("</style>")[1]


def test_export_without_page_numbers_knits_words(tmp_path, doc):
    from unscanner.assemble import ExportStyle

    doc.page(1).html = "<h1>T</h1><p>We speak of self-government. An inter-</p>"
    doc.page(2).html = "<p>national order of self-</p>"  # flags not set: the broken word says it continues
    doc.page(3).html = "<p>government.</p><p>Last.</p>"
    doc.page(3).starts_mid_paragraph = doc.page(2).ends_mid_paragraph = True
    for n, p in enumerate(doc.pages):
        p.status, p.label = "done", str(10 + n)
    a = assemble(doc, tmp_path / "out")
    with_numbers = a.html()
    assert with_numbers.count('id="pg-') == 3 and "inter<span" in with_numbers
    plain = a.html(ExportStyle(indent=True, justify=True, page_numbers=False))
    assert 'id="pg-' not in plain and "page-list" not in plain.split("</style>")[1]
    assert "An international order of self-government.</p>" in plain
    assert "text-indent: 1.5em" in plain and "text-align: justify" in plain
    assert a.html().count('id="pg-') == 3  # the assembled document itself is untouched

    epub = build_epub(a, tmp_path / "plain.epub", style=ExportStyle(page_numbers=False))
    with zipfile.ZipFile(epub) as z:
        text = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("OEBPS/text/"))
        assert "pagebreak" not in text and "An international order of self-government." in text
        assert "page-list" not in z.read("OEBPS/nav.xhtml").decode()
        assert "printPageNumbers" not in z.read("OEBPS/package.opf").decode()
        assert "text-indent" not in z.read("OEBPS/style.css").decode()


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
    monkeypatch.setenv("UNSCANNER_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("UNSCANNER_OUT_DIR", str(tmp_path / "out"))
    from mcp.client.client import Client

    from unscanner.mcp_server import server

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


def test_coverage_counts_compact_table_cells_as_separate_words(doc):
    # an election-results page: cells with no whitespace between the tags must not run together
    names = ["Smith", "Jones", "Brown", "Clark", "Adams", "Baker", "Evans", "Green"]
    rows = "".join(f'<tr><th scope="row">{n}</th><td>1,{400 + i}</td><td>{60 + i}</td></tr>'
                   for i, n in enumerate(names))
    p = doc.page(1)
    p.status = "needs_review"
    p.html = f'<table><tr><th scope="col">Name</th><th scope="col">Votes</th><th scope="col">Majority</th></tr>{rows}</table>'
    p.draft_text = "Name Votes Majority\n" + "\n".join(f"{n} 1,{400 + i} {60 + i}" for i, n in enumerate(names))
    assert not [i for i in coverage(doc) if i.location == "page 1"]


def test_page_load_failure_is_a_page_error(doc, monkeypatch):
    import unscanner.pipeline as pl

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
    import unscanner.pipeline as pl

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
    import unscanner.pipeline as pl

    pl.ensure_draft_text(doc, [1, 2, 3])
    calls = []

    class Slow(FakeBackend):
        def transcribe(self, png, prompt, system=None):
            calls.append(prompt)
            _time.sleep(0.05)
            return super().transcribe(png, prompt)

    def bad_save():
        raise PermissionError("doc.json is locked")

    monkeypatch.setattr(doc, "save", bad_save)
    with pytest.raises(PermissionError):
        transcribe_pages(doc, Slow(), [1, 2, 3], workers=1)
    assert len(calls) < 3


def _pages(doc, *pages):
    """Store (label, html, starts_mid, ends_mid) on the document's pages, as if transcribed."""
    for page, (label, html, starts, ends) in zip(doc.pages, pages):
        page.label, page.html, page.status = label, html, "needs_review"
        page.starts_mid_paragraph, page.ends_mid_paragraph = starts, ends
    return doc


def test_numbered_list_split_inside_an_item_keeps_counting(doc):
    _pages(doc,
           ("1", "<h1>T</h1><ol><li>one</li><li>two</li><li>three starts</li></ol>", False, True),
           ("2", '<ol start="3"><li>and ends</li><li>four</li></ol><p>After.</p>', True, False),
           ("3", "<p>End.</p>", False, False))
    a = assemble(doc)
    html = lhtml.tostring(a.main, encoding="unicode")  # just the content, not the page-list <ol>
    assert html.count("<ol") == 1 and "start=" not in html
    assert '<li>three starts <span role="doc-pagebreak" id="pg-2"' in html
    assert html.index("and ends</li><li>four</li></ol>") < html.index("<p>After.</p>")
    assert html.count('role="doc-pagebreak"') == 3 and not a.warnings


def test_numbered_list_split_between_items_is_joined_by_its_start(doc):
    _pages(doc,
           ("1", '<h1>T</h1><ol type="a"><li>one</li><li>two</li></ol>'
                 '<aside role="doc-footnote" id="fn-1-1"><p>1. Note.</p></aside>', False, False),
           ("2", '<ol type="a" start="3"><li>three</li><li>four</li></ol>', False, False),
           ("3", '<ol type="a" start="9"><li>unrelated</li></ol>', False, False))
    a = assemble(doc)
    html = lhtml.tostring(a.main, encoding="unicode")  # just the content, not the page-list <ol>
    assert '<li>two</li><li><span role="doc-pagebreak" id="pg-2"' in html
    assert html.index("<li>four</li></ol>") < html.index("doc-footnote")
    # page 3's numbers do not follow on: it stays a list of its own, keeps its start, and is reported
    assert '<ol type="a" start="9">' in html and html.count("<ol") == 2
    assert html.count('role="doc-pagebreak"') == 3
    assert [w for w in a.warnings if w.startswith("page 3:") and "not joined" in w]
    assert not [i for i in validate_html(a.html()) if i.severity == "error"]


def test_paragraph_split_over_a_page_with_footnotes_joins_the_body_not_the_footnote(doc):
    _pages(doc,
           ("1", '<h1>T</h1><p>The sentence starts</p>'
                 '<aside role="doc-footnote" id="fn-1-1"><p>1. Note.</p></aside>', False, True),
           ("2", "<p>and ends here.</p><p>Next.</p>", True, False),
           ("3", "<p>End.</p>", False, False))
    a = assemble(doc)
    html = lhtml.tostring(a.main, encoding="unicode")
    assert '<p>The sentence starts <span role="doc-pagebreak" id="pg-2"' in html
    assert "</span>and ends here.</p>" in html
    assert '<aside role="doc-footnote" id="fn-1-1"><p>1. Note.</p></aside>' in html
    assert html.index("and ends here.") < html.index("doc-footnote") < html.index("<p>Next.</p>")
    assert html.count('role="doc-pagebreak"') == 3 and not a.warnings


def test_continuation_is_never_taken_from_a_footnote_opening_the_new_page(doc):
    _pages(doc,
           ("1", "<h1>T</h1><p>The sentence starts</p>", False, True),
           ("2", '<aside role="doc-footnote" id="fn-2-1"><p>1. Note.</p></aside><p>Body.</p>', True, False),
           ("3", "<p>End.</p>", False, False))
    a = assemble(doc)
    html = lhtml.tostring(a.main, encoding="unicode")
    assert '<p>The sentence starts <span role="doc-pagebreak" id="pg-2"' in html and "</span>Body.</p>" in html
    assert '<aside role="doc-footnote" id="fn-2-1"><p>1. Note.</p></aside>' in html
    assert html.count('role="doc-pagebreak"') == 3


def test_heading_over_two_lines_reads_as_one_line(tmp_path, doc):
    """A <br> inside a heading is a printed line break, not a word boundary to swallow."""
    from unscanner.assemble import heading_text
    from lxml import html as lhtml

    assert heading_text(lhtml.fragment_fromstring("<h2>Chapter IX.<br>The Civil War</h2>")) == \
        "Chapter IX. The Civil War"
    assert heading_text(lhtml.fragment_fromstring("<h2>Chapter\n  IX. <br/> The <em>Civil</em> War</h2>")) == \
        "Chapter IX. The Civil War"

    doc.page(1).html = "<h1>Sample Book</h1><p>a</p>"
    doc.page(2).html = "<h2>Chapter IX.<br>The Civil War</h2><p>b</p>"
    doc.page(3).html = "<p>c</p>"
    for n, p in enumerate(doc.pages):
        p.status, p.label = "done", str(10 + n)
    a = assemble(doc, tmp_path / "out")
    epub = build_epub(a, tmp_path / "lines.epub")
    with zipfile.ZipFile(epub) as z:
        nav = z.read("OEBPS/nav.xhtml").decode()
        assert "Chapter IX. The Civil War" in nav and "IX.The" not in nav
        # ...and in the chapter file's <title>, which comes from the same heading
        assert any("<title>Chapter IX. The Civil War</title>" in z.read(n).decode()
                   for n in z.namelist() if n.startswith("OEBPS/text/"))
