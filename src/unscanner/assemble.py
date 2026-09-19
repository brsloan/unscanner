"""Assemble per-page HTML fragments into one accessible HTML document.

Responsibilities: page-break markers (DPUB-ARIA doc-pagebreak) that keep printed page numbers
citable, merging paragraphs split across pages, heading-level normalization (exactly one h1, no
skipped levels), figure cropping, id de-duplication, and the final HTML wrapper.
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree, html as lhtml

from .document import Document, Page, slugify
from .pdf import cached_page_png, crop_png
from .sanitize import ALLOWED_CLASSES

TEXT_BLOCKS = {"p", "li", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6"}
CONTAINERS = {"blockquote", "ul", "ol", "section", "aside", "dl", "div"}
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]

CSS = """
:root { color-scheme: light dark; }
body { max-width: 42em; margin: 2em auto; padding: 0 1em; font-family: Georgia, "Times New Roman", serif;
       font-size: 1.05rem; line-height: 1.55; }
h1, h2, h3, h4, h5, h6 { font-family: system-ui, sans-serif; line-height: 1.25; }
[role="doc-pagebreak"] { font-family: system-ui, sans-serif; font-size: 0.75em; color: #4a4a4a;
       border: 1px solid #8a8a8a; border-radius: 3px; padding: 0 0.35em; margin: 0 0.35em; white-space: nowrap; }
div[role="doc-pagebreak"] { display: block; width: max-content; margin: 1.5em 0 0.5em; }
@media (prefers-color-scheme: dark) { [role="doc-pagebreak"] { color: #c8c8c8; border-color: #8a8a8a; } }
aside[role="doc-footnote"] { font-size: 0.9em; border-top: 1px solid #8a8a8a; margin-top: 1em; padding-top: 0.5em; }
figure { margin: 1.5em 0; } figure img { max-width: 100%; height: auto; }
figcaption { font-size: 0.9em; font-style: italic; }
th[scope="row"] { text-align: left; }
.align-left { text-align: left; } .align-center, th.align-center { text-align: center; } .align-right, th.align-right { text-align: right; }
figure.wrap.align-left { float: left; max-width: 45%; margin: 0.3em 1.2em 0.8em 0; }
figure.wrap.align-right { float: right; max-width: 45%; margin: 0.3em 0 0.8em 1.2em; }
h1, h2, h3, h4, h5, h6, table, [role="doc-pagebreak"] { clear: both; }
table { border-collapse: collapse; margin: 1em 0; } th, td { border: 1px solid #8a8a8a; padding: 0.3em 0.6em; }
nav.page-list ol { columns: 6 5em; list-style: none; padding: 0; }
.skip-link { position: absolute; left: -999px; } .skip-link:focus { left: 1em; top: 1em; }
"""


@dataclass
class Assembled:
    main: lhtml.HtmlElement  # <main> element holding the whole document body
    title: str
    language: str
    figures: dict[str, Path] = field(default_factory=dict)  # figure file name -> path on disk
    page_ids: list[tuple[str, str]] = field(default_factory=list)  # (id, label) in order
    warnings: list[str] = field(default_factory=list)

    def html(self) -> str:
        return wrap_html(self)


# ---------------------------------------------------------------- fragments

def parse_fragment(fragment: str) -> lhtml.HtmlElement:
    """Parse a body fragment into a <section> wrapper element."""
    frag = fragment.strip() or "<p></p>"
    try:
        return lhtml.fragment_fromstring(frag, create_parent="section")
    except (etree.ParserError, ValueError):
        return lhtml.fragment_fromstring(htmlmod.escape(frag), create_parent="section")


def _edge_leaf(el, pick):
    """Descend through container blocks (blockquote, lists, sections) to the first/last text block.

    pick(el) returns the child to descend into (first or last). Returns None when the edge child is
    not a text-bearing block (e.g. a table, figure or page marker)."""
    cur = el
    while len(cur):
        child = pick(cur)
        if not isinstance(child.tag, str):
            return None
        if child.tag == "li" and len(child) and child[-1 if pick is _last else 0].tag in TEXT_BLOCKS | CONTAINERS:
            cur = child
            continue
        if child.tag in TEXT_BLOCKS:
            return child
        if child.tag in CONTAINERS and child.get("role") != "doc-pagebreak":
            cur = child
            continue
        return None
    return None


def _last(el):
    return el[-1]


def _first(el):
    return el[0]


def _last_leaf(el):
    return _edge_leaf(el, _last)


def _first_leaf(el):
    return _edge_leaf(el, _first)


def _tail_text(el) -> str:
    """Text at the very end of an element (tail of last child, or text)."""
    if len(el):
        return el[-1].tail or ""
    return el.text or ""


def _set_tail_text(el, value: str) -> None:
    if len(el):
        el[-1].tail = value
    else:
        el.text = value


def _remove_and_prune(el, stop) -> None:
    """Remove el and any ancestors left empty (no text, no children), stopping at `stop`."""
    parent = el.getparent()
    parent.remove(el)
    while parent is not None and parent is not stop and not len(parent) and not (parent.text or "").strip():
        gp = parent.getparent()
        gp.remove(parent)
        parent = gp


def merge_continuation(prev_section, cur_section, marker) -> bool:
    """Try to join the last block of prev_section with the first block of cur_section.

    On success the inline `marker` is placed at the join and cur's first block is removed.
    """
    last = _last_leaf(prev_section)
    first = _first_leaf(cur_section)
    if last is None or first is None:
        return False
    if last.tag != first.tag and not (last.tag in {"p", "li", "dd"} and first.tag in {"p", "li", "dd"}):
        return False
    if last.tag.startswith("h"):
        return False
    tail = _tail_text(last).rstrip()
    lead = (first.text or "")
    joiner = " "
    if tail.endswith("-") and lead[:1].islower():  # un-dehyphenated line break at the page edge
        tail = tail[:-1]
        joiner = ""
    _set_tail_text(last, tail + (" " if joiner else ""))
    last.append(marker)
    marker.tail = lead.lstrip() if not joiner else lead
    for child in list(first):
        last.append(child)
    _remove_and_prune(first, cur_section)
    return True


def make_marker(page_id: str, label: str, inline: bool):
    el = lhtml.Element("span" if inline else "div")
    el.set("role", "doc-pagebreak")
    el.set("id", page_id)
    el.set("aria-label", f"Page {label}")
    el.set("title", f"Page {label}")
    el.text = label
    return el


# ---------------------------------------------------------------- figures

def resolve_figures(doc: Document, page: Page, section, out_dir: Path, figures: dict[str, Path],
                    warnings: list[str]) -> None:
    by_id = {f.id: f for f in page.figures}
    for img in section.iter("img"):
        src = img.get("src", "")
        fid = src[4:] if src.startswith("fig:") else src
        fig = by_id.get(fid)
        alt = img.get("alt") or (fig.alt if fig else "")
        if fig and fig.bbox and out_dir is not None:
            name = f"p{page.index:03d}-{slugify(fid)}.png"
            path = out_dir / "figures" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(crop_png(cached_page_png(doc, page.index).read_bytes(), fig.bbox, fig.rotate))
            figures[name] = path
            img.set("src", f"figures/{name}")
            img.set("alt", alt)
        else:
            # No crop available: replace the image with its description so nothing is lost.
            p = lhtml.Element("p")
            p.set("class", "figure-description")
            strong = lhtml.Element("strong")
            p.append(strong)
            strong.text = "Image: "
            strong.tail = alt or "(no description)"
            p.tail = img.tail
            img.getparent().replace(img, p)
            if not alt:
                warnings.append(f"page {page.index}: image without description")


# ---------------------------------------------------------------- headings

def _level(tag: str) -> int:
    return int(tag[1])


def normalize_headings(main, title: str) -> None:
    heads = [el for el in main.iter() if isinstance(el.tag, str) and el.tag in HEADINGS]
    h1s = [h for h in heads if h.tag == "h1"]
    need_title = False
    if len(h1s) == 0:
        need_title = True
    elif len(h1s) > 1:
        # Several chapter titles: demote everything one level and add the document title as h1.
        for h in heads:
            h.tag = f"h{min(6, _level(h.tag) + 1)}"
        need_title = True
    if need_title:
        h = lhtml.Element("h1")
        h.text = title
        main.insert(0, h)
        heads.insert(0, h)
    # Close skipped levels (h2 -> h4 becomes h2 -> h3).
    prev = 1
    for h in heads:
        lvl = _level(h.tag)
        if lvl > prev + 1:
            lvl = prev + 1
            h.tag = f"h{lvl}"
        prev = lvl


def assign_heading_ids(main) -> None:
    n = 0
    for el in main.iter():
        if isinstance(el.tag, str) and el.tag in HEADINGS and not el.get("id"):
            n += 1
            el.set("id", f"h-{n}")


def dedupe_ids(main, warnings: list[str]) -> None:
    seen: set[str] = set()
    for el in main.iter():
        if not isinstance(el.tag, str):
            continue
        i = el.get("id")
        if not i:
            continue
        if i in seen:
            n = 2
            while f"{i}-{n}" in seen:
                n += 1
            warnings.append(f"duplicate id {i!r} renamed to {i}-{n}")
            i = f"{i}-{n}"
            el.set("id", i)
        seen.add(i)


def strip_disallowed(main, warnings: list[str]) -> None:
    """Remove presentational leftovers a model might still emit."""
    for el in main.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("style", "align", "width", "height", "bgcolor", "font"):
            if attr in el.attrib:
                del el.attrib[attr]
        if "class" in el.attrib:
            keep = [c for c in el.get("class", "").split() if c in ALLOWED_CLASSES]
            if keep:
                el.set("class", " ".join(keep))
            else:
                del el.attrib["class"]
        if el.tag in ("b", "i", "font", "center"):
            el.tag = {"b": "strong", "i": "em"}.get(el.tag, "span")
    for el in list(main.iter("script", "style", "link", "meta")):
        el.getparent().remove(el)
        warnings.append(f"removed <{el.tag}> element")


# ---------------------------------------------------------------- main entry

def assemble(doc: Document, out_dir: str | Path | None = None) -> Assembled:
    out_dir = Path(out_dir) if out_dir is not None else None
    main = lhtml.Element("main")
    main.set("id", "content")
    figures: dict[str, Path] = {}
    warnings: list[str] = []
    page_ids: list[tuple[str, str]] = []
    prev_page: Page | None = None
    used_ids: set[str] = set()

    for page in doc.pages:
        if page.skip or not page.html.strip():
            if not page.skip and page.status == "pending":
                warnings.append(f"page {page.index}: not transcribed yet")
            continue
        label = page.label or str(page.index)
        pid = "pg-" + slugify(label)
        while pid in used_ids:
            pid += "b"
        used_ids.add(pid)
        page_ids.append((pid, label))

        section = parse_fragment(page.html)
        resolve_figures(doc, page, section, out_dir, figures, warnings)

        merged = False
        if prev_page is not None and prev_page.ends_mid_paragraph and page.starts_mid_paragraph:
            # The previous page's blocks are the trailing children of <main>.
            merged = merge_continuation(main, section, make_marker(pid, label, inline=True))
        if not merged:
            main.append(make_marker(pid, label, inline=False))
        if section.text and section.text.strip():  # bare text before the first element
            p = lhtml.Element("p")
            p.text = section.text.strip()
            main.append(p)
        for child in list(section):
            main.append(child)
        prev_page = page

    strip_disallowed(main, warnings)
    normalize_headings(main, doc.title)
    assign_heading_ids(main)
    dedupe_ids(main, warnings)
    return Assembled(main=main, title=doc.title, language=doc.language, figures=figures,
                     page_ids=page_ids, warnings=warnings)


# ---------------------------------------------------------------- wrapper

def page_list_nav(page_ids: list[tuple[str, str]]) -> str:
    if not page_ids:
        return ""
    items = "".join(f'<li><a href="#{pid}">{htmlmod.escape(label)}</a></li>' for pid, label in page_ids)
    return ('<nav class="page-list" aria-label="Page list"><details><summary>Page list</summary>'
            f"<ol>{items}</ol></details></nav>")


def wrap_html(a: Assembled) -> str:
    body = lhtml.tostring(a.main, encoding="unicode", method="html", pretty_print=True)
    title = htmlmod.escape(a.title or "Document")
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{htmlmod.escape(a.language)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        '<a class="skip-link" href="#content">Skip to content</a>\n'
        f"{body}\n{page_list_nav(a.page_ids)}\n</body>\n</html>\n"
    )


def main_text(main) -> str:
    return re.sub(r"\s+", " ", main.text_content()).strip()
