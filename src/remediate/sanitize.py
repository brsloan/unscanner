"""Normalize HTML coming from the editor (or a model) to the small semantic vocabulary we allow.

Everything else is unwrapped (text kept) or dropped (script/style). This is what guarantees that a
human editing in the browser cannot produce inaccessible markup: bold becomes <strong>, divs
become paragraphs, inline styles and classes disappear.
"""

from __future__ import annotations

from lxml import etree, html as lhtml

ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote", "table", "caption", "thead",
    "tbody", "tfoot", "tr", "th", "td", "figure", "figcaption", "img", "em", "strong", "cite", "sup", "sub",
    "dl", "dt", "dd", "pre", "code", "hr", "span", "aside", "section", "a", "br", "math", "del", "ins", "abbr",
}
GLOBAL_ATTRS = {"id", "lang"}
TAG_ATTRS = {
    "a": {"href", "role"},
    "img": {"src", "alt"},
    "th": {"scope", "colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
    "aside": {"role", "aria-label"},
    "section": {"aria-label"},
    "abbr": {"title"},
    "math": {"display", "xmlns"},
}
RENAME = {"b": "strong", "i": "em", "strike": "del", "s": "del"}
DROP = {"script", "style", "link", "meta", "iframe", "object", "embed", "form", "input", "button", "select"}
BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote", "table", "figure", "dl",
          "pre", "hr", "aside", "section", "div"}
MATH_TAGS_PREFIX = ("m",)  # MathML children (mi, mo, mn, mrow, msup, ...) are kept inside <math>


def _inside_math(el) -> bool:
    p = el.getparent()
    while p is not None:
        if p.tag == "math":
            return True
        p = p.getparent()
    return False


def sanitize_fragment(fragment: str) -> str:
    frag = (fragment or "").strip()
    if not frag:
        return ""
    try:
        root = lhtml.fragment_fromstring(frag, create_parent="div")
    except (etree.ParserError, ValueError):
        return ""

    # Post-order so children are settled before a parent is unwrapped.
    for el in reversed(list(root.iter())):
        if el is root:
            continue
        if not isinstance(el.tag, str):  # comments, processing instructions
            el.drop_tree()
            continue
        tag = el.tag.lower()
        if tag in DROP:
            el.drop_tree()
            continue
        if _inside_math(el):
            el.attrib.clear()
            continue
        if tag in RENAME:
            tag = RENAME[tag]
            el.tag = tag
        if tag == "div":
            # Browsers emit <div> for new lines in contenteditable: treat as a paragraph unless it
            # already wraps block content, in which case just unwrap it.
            if any(isinstance(c.tag, str) and c.tag in BLOCKS for c in el):
                el.drop_tag()
                continue
            el.tag = tag = "p"
        if tag == "span" and not el.get("lang"):
            el.drop_tag()
            continue
        if tag not in ALLOWED_TAGS:
            el.drop_tag()
            continue
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        for name in list(el.attrib):
            if name not in allowed:
                del el.attrib[name]
        if tag == "a" and el.get("href", "").lower().startswith(("javascript:", "data:")):
            del el.attrib["href"]
        if tag == "img" and el.get("alt") is None:
            el.set("alt", "")

    # Remove paragraphs and headings that are completely empty.
    for el in reversed(list(root.iter("p", "h1", "h2", "h3", "h4", "h5", "h6", "li"))):
        if not el.text_content().strip() and not list(el.iter("img", "br", "math")):
            el.drop_tree()

    parts = [root.text or ""]
    parts += [lhtml.tostring(c, encoding="unicode", method="html") for c in root]
    return "".join(parts).strip()
