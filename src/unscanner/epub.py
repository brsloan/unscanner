"""Write an EPUB 3 (with EPUB Accessibility 1.1 metadata, TOC and page-list) from an assembled document."""

from __future__ import annotations

import copy
import datetime as dt
import uuid
import zipfile
from pathlib import Path

from lxml import etree, html as lhtml

from .assemble import HEADINGS, Assembled, ExportStyle, export_style, heading_text, without_page_markers

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
SPLIT_AT = {"h1", "h2"}


def _xhtml_doc(title: str, lang: str, body_children, css_href: str = "../style.css") -> bytes:
    root = etree.Element("{%s}html" % XHTML_NS, nsmap={None: XHTML_NS, "epub": EPUB_NS})
    root.set("lang", lang)
    root.set("{http://www.w3.org/XML/1998/namespace}lang", lang)
    head = etree.SubElement(root, "{%s}head" % XHTML_NS)
    t = etree.SubElement(head, "{%s}title" % XHTML_NS)
    t.text = title
    link = etree.SubElement(head, "{%s}link" % XHTML_NS)
    link.set("rel", "stylesheet")
    link.set("type", "text/css")
    link.set("href", css_href)
    body = etree.SubElement(root, "{%s}body" % XHTML_NS)
    section = etree.SubElement(body, "{%s}section" % XHTML_NS)
    section.set("{%s}type" % EPUB_NS, "bodymatter chapter")
    for child in body_children:
        section.append(_to_xhtml(child))
    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def _to_xhtml(el):
    """Deep-copy an lxml.html element into the XHTML namespace, adding epub:type where useful."""
    if not isinstance(el.tag, str):
        return copy.deepcopy(el)
    new = etree.Element("{%s}%s" % (XHTML_NS, el.tag))
    for k, v in el.attrib.items():
        new.set(k, v)
    role = el.get("role", "")
    if role == "doc-pagebreak":
        new.set("{%s}type" % EPUB_NS, "pagebreak")
    elif role == "doc-footnote":
        new.set("{%s}type" % EPUB_NS, "footnote")
    elif role == "doc-noteref":
        new.set("{%s}type" % EPUB_NS, "noteref")
    elif role == "doc-backlink":
        new.set("{%s}type" % EPUB_NS, "backlink")
    new.text = el.text
    new.tail = el.tail
    for child in el:
        new.append(_to_xhtml(child))
    return new


def _split_chapters(main) -> list[list]:
    chunks: list[list] = [[]]
    for child in main:
        if isinstance(child.tag, str) and child.tag in SPLIT_AT and chunks[-1]:
            chunks.append([])
        chunks[-1].append(child)
    return [c for c in chunks if c]


def _chapter_title(chunk, fallback: str) -> str:
    for el in chunk:
        if isinstance(el.tag, str) and el.tag in HEADINGS:
            return heading_text(el) or fallback
    return fallback


def build_epub(a: Assembled, out_path: str | Path, author: str = "", source: str = "",
               style: ExportStyle | None = None) -> Path:
    out_path = Path(out_path)
    style = style or export_style("epub")
    # Without page numbers there are no markers, so no page-list and no page features in the metadata.
    main = copy.deepcopy(a.main) if style.page_numbers else without_page_markers(a.main)
    # Chapter files live in OEBPS/text/, images in OEBPS/figures/: make the image paths relative to text/.
    for im in main.iter("img"):
        src = im.get("src", "")
        if src.startswith("figures/"):
            im.set("src", "../" + src)
    chunks = _split_chapters(main)
    files = [f"text/ch{i + 1:03d}.xhtml" for i in range(len(chunks))]

    # id -> file map so cross-chapter fragment links keep working.
    id_file: dict[str, str] = {}
    for fname, chunk in zip(files, chunks):
        for el in chunk:
            for e in ([el] if isinstance(el.tag, str) else []) + [x for x in el.iter() if isinstance(x.tag, str)]:
                if e.get("id"):
                    id_file[e.get("id")] = fname
    for fname, chunk in zip(files, chunks):
        for el in chunk:
            for e in el.iter("a"):
                href = e.get("href", "")
                if href.startswith("#"):
                    target = id_file.get(href[1:])
                    if target and target != fname:
                        e.set("href", f"{Path(target).name}{href}")

    # TOC entries (h1-h3) and page list, in document order. hrefs are relative to OEBPS/nav.xhtml.
    toc: list[tuple[int, str, str]] = []
    pages: list[tuple[str, str]] = []
    for fname, chunk in zip(files, chunks):
        for el in chunk:
            for e in ([el] if isinstance(el.tag, str) else []) + [x for x in el.iter() if isinstance(x.tag, str)]:
                if e.tag in ("h1", "h2", "h3") and e.get("id"):
                    toc.append((int(e.tag[1]), heading_text(e), f"{fname}#{e.get('id')}"))
                if e.get("role") == "doc-pagebreak" and e.get("id"):
                    pages.append((e.text or e.get("aria-label", ""), f"{fname}#{e.get('id')}"))
    # de-duplicate nested traversal results while keeping order
    seen = set()
    toc = [t for t in toc if not (t[2] in seen or seen.add(t[2]))]
    seen = set()
    pages = [p for p in pages if not (p[1] in seen or seen.add(p[1]))]

    has_images = any(True for _ in main.iter("img"))
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    nav_xhtml = _nav_doc(a.title, a.language, toc, pages, files[0] if files else "")
    opf = _package_opf(a, book_id, modified, author, source, files, has_images, bool(pages))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/package.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", style.css(), compress_type=zipfile.ZIP_DEFLATED)
        for fname, chunk in zip(files, chunks):
            title = _chapter_title(chunk, a.title)
            z.writestr(f"OEBPS/{fname}", _xhtml_doc(title, a.language, chunk), compress_type=zipfile.ZIP_DEFLATED)
        for name, path in a.figures.items():
            z.write(path, f"OEBPS/figures/{name}", compress_type=zipfile.ZIP_DEFLATED)
    return out_path


CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_toc(entries: list[tuple[int, str, str]]) -> str:
    """Nested <ol> from a flat (level, text, href) list. Deeper jumps are clamped to one level."""
    out: list[str] = []

    def rec(i: int, level: int) -> int:
        out.append("<ol>")
        while i < len(entries):
            lvl, text, href = entries[i]
            if lvl < level:
                break
            out.append(f'<li><a href="{_esc(href)}">{_esc(text)}</a>')
            i += 1
            if i < len(entries) and entries[i][0] > lvl:
                i = rec(i, lvl + 1)
            out.append("</li>")
        out.append("</ol>")
        return i

    # Normalize levels so the list starts at 1 and never jumps by more than one (an h2 that
    # precedes the first h1, e.g. a cover sheet, becomes a top-level entry).
    norm: list[tuple[int, str, str]] = []
    prev = 0
    for lvl, text, href in entries:
        lvl = min(lvl, prev + 1) if prev else 1
        norm.append((lvl, text, href))
        prev = lvl
    entries = norm
    if entries:
        rec(0, 1)
    return "".join(out)


def _nav_doc(title: str, lang: str, toc, pages, first_file: str) -> bytes:
    toc_html = render_toc(toc) if toc else f'<ol><li><a href="{_esc(first_file)}">{_esc(title)}</a></li></ol>'
    page_html = ""
    if pages:
        items = "".join(f'<li><a href="{_esc(h)}">{_esc(t)}</a></li>' for t, h in pages)
        page_html = f'<nav epub:type="page-list" aria-label="Page list" hidden=""><h2>Page list</h2><ol>{items}</ol></nav>'
    landmarks = (f'<nav epub:type="landmarks" aria-label="Landmarks" hidden=""><h2>Landmarks</h2><ol>'
                 f'<li><a epub:type="bodymatter" href="{_esc(first_file)}">Start of content</a></li>'
                 f"</ol></nav>") if first_file else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{_esc(lang)}" xml:lang="{_esc(lang)}">'
        f"<head><title>{_esc(title)}</title><link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"/></head><body>"
        f'<nav epub:type="toc" id="toc" aria-label="Table of contents"><h1>Contents</h1>{toc_html}</nav>'
        f"{page_html}{landmarks}</body></html>"
    ).encode("utf-8")


def _package_opf(a: Assembled, book_id: str, modified: str, author: str, source: str, files, has_images: bool,
                 has_pages: bool) -> str:
    features = ["structuralNavigation", "readingOrder", "tableOfContents"]
    if has_pages:
        features += ["pageNavigation", "printPageNumbers", "pageBreakMarkers"]
    if has_images:
        features.append("alternativeText")
    meta = [
        f"<dc:identifier id=\"bookid\">{_esc(book_id)}</dc:identifier>",
        f"<dc:title>{_esc(a.title)}</dc:title>",
        f"<dc:language>{_esc(a.language)}</dc:language>",
        f"<meta property=\"dcterms:modified\">{modified}</meta>",
    ]
    if author:
        meta.append(f"<dc:creator>{_esc(author)}</dc:creator>")
    if source:
        meta.append(f"<dc:source>{_esc(source)}</dc:source>")
    meta.append("<meta property=\"schema:accessMode\">textual</meta>")
    if has_images:
        meta.append("<meta property=\"schema:accessMode\">visual</meta>")
    meta.append("<meta property=\"schema:accessModeSufficient\">textual</meta>")
    meta += [f"<meta property=\"schema:accessibilityFeature\">{f}</meta>" for f in features]
    meta.append("<meta property=\"schema:accessibilityHazard\">none</meta>")
    meta.append("<meta property=\"schema:accessibilitySummary\">Semantic HTML with headings"
                f"{', navigable print page numbers' if has_pages else ''} and image descriptions, produced from a scanned original; conforms to "
                "WCAG 2.1 Level AA as far as automated checks can verify.</meta>")
    meta.append("<link rel=\"dcterms:conformsTo\" href=\"http://www.idpf.org/epub/a11y/accessibility-20170105.html#wcag-aa\"/>")
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    spine = []
    for i, f in enumerate(files):
        cid = f"ch{i + 1:03d}"
        props = ""
        manifest.append(f'<item id="{cid}" href="{f}" media-type="application/xhtml+xml"{props}/>')
        spine.append(f'<itemref idref="{cid}"/>')
    for j, name in enumerate(a.figures):
        manifest.append(f'<item id="img{j + 1}" href="figures/{_esc(name)}" media-type="image/png"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
        f'xml:lang="{_esc(a.language)}" prefix="schema: http://schema.org/">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{"".join(meta)}</metadata>'
        f'<manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>'
    )
