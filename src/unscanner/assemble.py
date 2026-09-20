"""Assemble per-page HTML fragments into one accessible HTML document.

Responsibilities: page-break markers (DPUB-ARIA doc-pagebreak) that keep printed page numbers
citable, merging paragraphs split across pages, heading-level normalization (exactly one h1, no
skipped levels), figure cropping, id de-duplication, and the final HTML wrapper.
"""

from __future__ import annotations

import base64
import copy
import html as htmlmod
import json
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

# Shared by the HTML and the EPUB writer. The 31em measure holds a line of about 66 characters in
# Georgia (whose average character is 0.443em) and about 72 in the Times New Roman fallback; an em
# measure rather than ch because Georgia's old-style figures make its ch 1.39 real characters wide.
CSS = """
:root { color-scheme: light dark; }
body { max-width: 31em; margin: 2em auto; padding: 0 1em; font-family: Georgia, "Times New Roman", serif;
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
.small-caps { font-variant: small-caps; }
figure.wrap.align-left { float: left; max-width: 45%; margin: 0.3em 1.2em 0.8em 0; }
figure.wrap.align-right { float: right; max-width: 45%; margin: 0.3em 0 0.8em 1.2em; }
h1, h2, h3, h4, h5, h6, table, [role="doc-pagebreak"] { clear: both; }
table { border-collapse: collapse; margin: 1em 0; } th, td { border: 1px solid #8a8a8a; padding: 0.3em 0.6em; }
nav.page-list ol { columns: 6 5em; list-style: none; padding: 0; }
.skip-link { position: absolute; left: -999px; } .skip-link:focus { left: 1em; top: 1em; }
"""

# Added to CSS when the format's "indent" export setting is on: paragraphs set the way a printed book
# sets them. No space between paragraphs; a first-line indent only on a paragraph that follows another
# paragraph, so not after a heading, a figure, a table, a list, a quotation or any other break in the
# text. A page marker that sits between two paragraphs does not count as a break.
BOOK_CSS = """
p { margin: 0; text-indent: 0; }
p + p, p + div[role="doc-pagebreak"] + p { text-indent: 1.5em; }
li p, td p, th p, figcaption p { text-indent: 0; }
p.align-center, p.align-right, div[role="doc-pagebreak"] + p.align-center,
div[role="doc-pagebreak"] + p.align-right { text-indent: 0; }
blockquote, ul, ol, dl { margin-top: 1em; margin-bottom: 1em; }
li ul, li ol { margin-top: 0; margin-bottom: 0; }
li p + p { margin-top: 0.5em; }
div[role="doc-pagebreak"] { margin: 0.4em 0 0.2em; }
"""

# Added when the format's "justify" export setting is on. Off by default: justified text is harder to
# read for some people (WCAG 1.4.8). The align-* classes are more specific and still win.
JUSTIFY_CSS = """
p, li, dd { text-align: justify; -webkit-hyphens: auto; hyphens: auto; }
"""

# Export settings, per output format. They live in work/settings.json next to the UI's other settings
# (the Export section of the Settings dialog) and every build reads them: UI, CLI and MCP.
EXPORT_DEFAULTS: dict[str, bool] = {
    "html_indent": False, "html_justify": False, "html_page_numbers": True, "html_embed_images": True,
    "epub_indent": True, "epub_justify": False, "epub_page_numbers": True,
}


@dataclass
class ExportStyle:
    indent: bool = False
    justify: bool = False
    page_numbers: bool = True
    embed_images: bool = False  # HTML only: figures inside the file as data: URIs (an EPUB keeps image files)

    def css(self) -> str:
        return CSS + (BOOK_CSS if self.indent else "") + (JUSTIFY_CSS if self.justify else "")


def export_style(fmt: str, settings: dict | None = None) -> ExportStyle:
    """The export settings of one format ("html" or "epub"), defaults where `settings` has none."""
    s = {**EXPORT_DEFAULTS, **{k: v for k, v in (settings or {}).items() if k in EXPORT_DEFAULTS}}
    return ExportStyle(bool(s[f"{fmt}_indent"]), bool(s[f"{fmt}_justify"]), bool(s[f"{fmt}_page_numbers"]),
                       bool(s.get(f"{fmt}_embed_images", False)))


def load_export_settings(work_root: str | Path) -> dict:
    """The export settings saved in <work_root>/settings.json; defaults when there is no such file."""
    try:
        return json.loads((Path(work_root) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class Assembled:
    main: lhtml.HtmlElement  # <main> element holding the whole document body
    title: str
    language: str
    figures: dict[str, Path] = field(default_factory=dict)  # figure file name -> path on disk
    page_ids: list[tuple[str, str]] = field(default_factory=list)  # (id, label) in order
    warnings: list[str] = field(default_factory=list)

    def html(self, style: ExportStyle | None = None) -> str:
        return wrap_html(self, style)


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
    not a text-bearing block (e.g. a table, figure or page marker). Footnotes are looked past: they
    sit at the end of a page's html, after the paragraph that may carry on over the page break."""
    cur = el
    while len(cur):
        child = pick(cur)
        if child is None or not isinstance(child.tag, str):
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


def _is_footnote(el) -> bool:
    return el.tag == "aside" and el.get("role") == "doc-footnote"


def _last(el):
    return next((c for c in reversed(el) if not _is_footnote(c)), None)


def _first(el):
    return next((c for c in el if not _is_footnote(c)), None)


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


_WORD = r"[^\W\d_]+"
_COMPOUND = _WORD + r"(?:[-‐]" + _WORD + ")*"  # "government" or "self-government"
_BROKEN_TAIL = re.compile(r"(?:(" + _COMPOUND + r")|\d)[-‐­]$")


def document_words(doc: Document) -> set[str]:
    """Every word of the document in lower case, hyphenated compounds as one word. It is what decides
    whether a hyphen at a page edge belongs to the word."""
    words: set[str] = set()
    for page in doc.pages:
        if not page.skip:
            text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", page.html or "")).lower().replace("‐", "-")
            words.update(re.findall(_COMPOUND, text))
    return words


def knit_word(tail: str, lead: str, words: set[str] | None = None) -> str | None:
    """Join a word broken over a page edge: `tail` is the text that ends the page, `lead` the text
    that opens the next one. Returns the new tail (the lead follows it with no space), or None when
    the page does not end in a broken word.

    "inter-" + "national" loses the hyphen, as the transcription guidelines ask for line ends. The
    hyphen stays when it belongs to the word: when the document spells the word with it somewhere
    else ("self-" + "government") and never without, when the piece before it is itself a compound
    ("mother-in-" + "law"), and before a capital or a digit ("Anglo-" + "Saxon", "1914-" + "1918")."""
    m = _BROKEN_TAIL.search(tail)
    if not m or not lead[:1].isalnum():
        return None
    if tail.endswith("­"):  # a soft hyphen is never part of the word
        return tail[:-1]
    start, rest = m.group(1), re.match(_WORD, lead)
    if start is None or rest is None or not lead[:1].islower():
        return tail
    joined = (start + rest.group()).lower()
    hyphenated = (start + "-" + rest.group()).lower().replace("‐", "-")
    words = words or set()
    if joined not in words and (hyphenated in words or "-" in start or "‐" in start):
        return tail
    return tail[:-1]


def word_broken(prev_section, cur_section) -> bool:
    """True when the page edge falls inside a hyphenated word, which makes the new page a continuation
    whatever its continuation flags say."""
    last, first = _last_leaf(prev_section), _first_leaf(cur_section)
    if last is None or first is None:
        return False
    return (first.text or "")[:1].islower() and bool(_BROKEN_TAIL.search(_tail_text(last).rstrip()))


def merge_continuation(prev_section, cur_section, marker, words: set[str] | None = None) -> bool:
    """Try to join the last block of prev_section with the first block of cur_section.

    On success the inline `marker` is placed at the join and cur's first block is removed. A word
    broken by the page edge is knitted together around the marker (see knit_word; `words` is the
    document's vocabulary), so it reads as one word when the markers are left out of an export.
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
    knitted = knit_word(tail, lead, words)
    if knitted is not None:  # un-dehyphenated line break at the page edge
        tail = knitted
        joiner = ""
    _set_tail_text(last, tail + (" " if joiner else ""))
    last.append(marker)
    marker.tail = lead.lstrip() if not joiner else lead
    for child in list(first):
        last.append(child)
    _carry_list_items(last, first)
    _remove_and_prune(first, cur_section)
    return True


def _carry_list_items(last, first) -> None:
    """After a list item split by the page break is joined, the items that follow it on the new page
    belong to the same list: move them over, so an <ol> keeps counting instead of restarting at 1.
    The same is done one level up while both sides are still inside matching lists (nested lists)."""
    while last is not None and first is not None and last.tag == "li" and first.tag == "li":
        prev_list, cur_list = last.getparent(), first.getparent()
        if prev_list.tag != cur_list.tag or prev_list.tag not in {"ol", "ul"}:
            return
        for item in list(first.itersiblings()):
            prev_list.append(item)
        last, first = prev_list.getparent(), cur_list.getparent()


def _ol_start(ol) -> int:
    try:
        return int(ol.get("start") or 1)
    except ValueError:
        return 1


def _closing_block(main):
    """The last block of the pages assembled so far, looking past the page's footnotes."""
    return _last(main)


def merge_continued_list(main, section, marker, where: str, warnings: list[str]) -> bool:
    """Join a numbered list that carries on over a page break that falls between two items.

    The new page says so itself: it opens with an <ol start="N"> where N is the number after the last
    item of the <ol> closing the previous page. Its items move into that list and the inline `marker`
    goes at the head of the first of them. Numbers that do not line up leave the lists apart."""
    prev = _closing_block(main)
    cur = section[0] if len(section) and not (section.text or "").strip() else None
    if prev is None or cur is None or prev.tag != "ol" or cur.tag != "ol" or cur.get("start") is None:
        return False
    items = [c for c in cur if c.tag == "li"]
    if not items or (prev.get("type") or "1") != (cur.get("type") or "1"):
        return False
    expected = _ol_start(prev) + sum(1 for c in prev if c.tag == "li")
    if _ol_start(cur) != expected:
        warnings.append(f"{where}: numbered list starts at {_ol_start(cur)} but the previous page's list "
                        f"ends at {expected - 1}; the two lists were not joined")
        return False
    host = items[0]
    if not (host.text or "").strip() and len(host) and host[0].tag == "p":
        host = host[0]  # an item made of paragraphs: the marker sits inside the first one
    marker.tail, host.text = host.text, None
    host.insert(0, marker)
    for child in list(cur):
        prev.append(child)
    section.remove(cur)
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


def heading_text(el) -> str:
    """A heading's text on one line.

    A heading printed over two lines is usually written with a <br> between them, and dropping the
    break runs the lines together ("Chapter IX.The Civil War"), so a break stands in as a space.
    """
    el = copy.deepcopy(el)
    for br in el.iter("br"):
        br.tail = " " + (br.tail or "")
    return re.sub(r"\s+", " ", el.text_content()).strip()


def outline(doc: Document) -> list[dict]:
    """The document's headings in reading order, for the UI's Headings sidebar.

    One entry per heading in a page's stored HTML, with the level as written (levels are only
    normalized when the document is built, and this has to match what the editor shows). `nth` is
    the heading's position among that page's headings, so clicking an entry can scroll the editor
    to the right one. Skipped pages contribute nothing: they are left out of the output.
    """
    items: list[dict] = []
    for page in doc.pages:
        if page.skip or not page.html:
            continue
        nth = 0
        for el in parse_fragment(page.html).iter():
            if not isinstance(el.tag, str) or el.tag not in HEADINGS:
                continue
            items.append({"level": _level(el.tag), "text": heading_text(el), "page": page.index,
                          "label": page.label, "nth": nth})
            nth += 1
    return items


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
    words = document_words(doc)

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
        if prev_page is not None and ((prev_page.ends_mid_paragraph and page.starts_mid_paragraph)
                                      or word_broken(main, section)):
            # The previous page's blocks are the trailing children of <main>.
            merged = merge_continuation(main, section, make_marker(pid, label, inline=True), words)
        if not merged and prev_page is not None:
            merged = merge_continued_list(main, section, make_marker(pid, label, inline=True),
                                          f"page {page.index}", warnings)
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


def without_page_markers(main):
    """A copy of <main> with the page markers taken out (an export with page numbers switched off).
    The text on both sides of an inline marker closes up, so a word the page edge fell in, already
    knitted around its marker by merge_continuation, reads as one word again."""
    main = copy.deepcopy(main)
    for el in [e for e in main.iter() if isinstance(e.tag, str) and e.get("role") == "doc-pagebreak"]:
        parent, prev, tail = el.getparent(), el.getprevious(), el.tail or ""
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
        parent.remove(el)
    return main


def with_embedded_images(main, figures: dict[str, Path]):
    """A copy of <main> with every built figure inside the document as a data: URI, so the HTML is one
    file that can be mailed, opened with a double-click or uploaded to a course site without its
    figures/ folder. A figure whose file cannot be read keeps its path."""
    main = copy.deepcopy(main)
    for img in main.iter("img"):
        path = figures.get((img.get("src") or "").removeprefix("figures/"))
        try:
            data = path.read_bytes() if path else None
        except OSError:
            data = None
        if data:
            img.set("src", "data:image/png;base64," + base64.b64encode(data).decode("ascii"))
    return main


def wrap_html(a: Assembled, style: ExportStyle | None = None) -> str:
    style = style or ExportStyle()
    main = a.main if style.page_numbers else without_page_markers(a.main)
    if style.embed_images:
        main = with_embedded_images(main, a.figures)
    body = lhtml.tostring(main, encoding="unicode", method="html", pretty_print=True)
    title = htmlmod.escape(a.title or "Document")
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{htmlmod.escape(a.language)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{style.css()}</style>\n</head>\n<body>\n"
        '<a class="skip-link" href="#content">Skip to content</a>\n'
        f"{body}\n{page_list_nav(a.page_ids if style.page_numbers else [])}\n</body>\n</html>\n"
    )


def main_text(main) -> str:
    return re.sub(r"\s+", " ", main.text_content()).strip()
