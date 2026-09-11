"""The transcription contract shared by every backend and by agents using the MCP server directly."""

from __future__ import annotations

import json
import re

GUIDELINES = """\
You are converting one page of a scanned document (usually a book chapter or article used as a
university course reading) into accessible semantic HTML that meets WCAG 2.1 AA. You are given the
page image (authoritative) and, when available, a draft OCR text (helpful but error-prone).
Read the image carefully; use the draft only to speed things up and to catch what you might miss.

OUTPUT: a single JSON object with these keys:
  label                 printed page number or label exactly as shown on the page ("38", "xii", "A-3"),
                        or null if none is visible.
  skip                  true only if the page has no meaningful content to transcribe (blank page,
                        an image-only cover with no text). Cover pages WITH a title/author are NOT skipped.
  starts_mid_paragraph  true if the first body text on the page continues a paragraph (or list item,
                        or block quote) that began on the previous page.
  ends_mid_paragraph    true if the last body text on the page is cut off and continues on the next page.
  html                  the page content as an HTML body fragment (no <html>, <head>, <body>).
  figures               list of {"id","alt","bbox","caption"} for every <img> you emitted (see below).
  notes                 short free text: anything illegible, uncertain, or that a human should check.
                        Empty string if nothing.

HTML RULES
- Transcribe ALL the body text on the page, word for word. Never summarize, shorten, or paraphrase.
  Preserve the author's spelling, punctuation and wording; fix only obvious OCR errors.
- Semantic elements only: h1-h6, p, ul/ol/li, blockquote, table/caption/thead/tbody/tr/th/td, figure,
  figcaption, img, em, strong, cite, sup, sub, dl/dt/dd, pre, code, hr, span, aside, section, a, br, math.
  No inline styles, no class-based styling, no <div> for layout, no <font>, no <b>/<i> (use strong/em).
- Join the lines of each paragraph into one <p>. Remove end-of-line hyphenation ("inter-" + newline +
  "national" becomes "international") but keep true hyphens ("self-government"). Never use <br> except
  inside verse.
- Drop running headers/footers, the printed page number, and any library/scanner stamps. Report the
  page number in "label" instead.
- Headings: the chapter or article title is <h1>; its major sections <h2>; subsections <h3>; and so on.
  Do not skip levels. A heading that merely continues from the previous page is not repeated.
  Do not turn emphasized words or the first line of a paragraph into headings.
- Italics -> <em>; bold -> <strong>; titles of works may use <cite>.
- Block quotations (indented or smaller type) -> <blockquote>. Lists -> ul/ol. Verse -> a <p> with <br>
  between lines.
- Footnote/endnote reference numbers in the text -> <sup><a href="#fn-LABEL-N" id="fnref-LABEL-N"
  role="doc-noteref">N</a></sup> where LABEL is the page label (or the word "page" plus the PDF page
  index if there is no label) and N the note number.
  If the note text itself is on this page (a footnote), put it at the end of the html as
  <aside role="doc-footnote" id="fn-LABEL-N"><p><a href="#fnref-LABEL-N" role="doc-backlink">N.</a> note
  text</p></aside>. If the notes live at the end of the chapter/book (endnotes), still emit the
  reference but as plain <sup>N</sup> without a link.
  A page that is itself a list of endnotes should be transcribed as an <ol> or <dl> of the notes
  (keep their numbers as text).
- Tables: real <table> with <caption> when there is a title, <th scope="col"/"row"> for header cells.
  Only use a table for genuinely tabular data, never for layout.
- Figures, photographs, charts, diagrams: emit <figure><img src="fig:LABEL-N" alt="..."><figcaption>
  caption text from the page</figcaption></figure> and add an entry to "figures" with the same id
  "LABEL-N", the alt text, and "bbox": [x0, y0, x1, y1] giving the image region in 0-1000 normalized
  page coordinates (0,0 top-left; 1000,1000 bottom-right). Write alt text that conveys what the image
  shows and why it matters in context (one to three sentences); if the caption already says it all,
  alt may be brief. Do not put the caption text in alt. Purely decorative ornaments are omitted.
- Text in another language: wrap in <span lang="xx">.
- Mathematics: simple expressions as plain text/Unicode; display equations as <math> MathML.
- Text in the image that is not body content (marginal notes by a reader, handwritten annotations)
  is omitted; mention it in notes.
- Escape &, <, > in text. Use straight or curly quotes as printed; do not "fix" them.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": ["string", "null"]},
        "skip": {"type": "boolean"},
        "starts_mid_paragraph": {"type": "boolean"},
        "ends_mid_paragraph": {"type": "boolean"},
        "html": {"type": "string"},
        "figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "alt": {"type": "string"},
                    "bbox": {"type": ["array", "null"], "items": {"type": "number"}},
                    "caption": {"type": "string"},
                },
                "required": ["id", "alt", "bbox", "caption"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["label", "skip", "starts_mid_paragraph", "ends_mid_paragraph", "html", "figures", "notes"],
    "additionalProperties": False,
}


def build_user_prompt(page_index: int, n_pages: int, draft_text: str, prev_tail: str, next_head: str,
                      doc_title: str, doc_language: str) -> str:
    parts = [
        f"Document: {doc_title or 'untitled'} (language: {doc_language}). "
        f"This is PDF page {page_index} of {n_pages}.",
    ]
    if prev_tail:
        parts.append("END OF THE PREVIOUS PAGE'S DRAFT TEXT (for continuation decisions only):\n" + prev_tail)
    if next_head:
        parts.append("START OF THE NEXT PAGE'S DRAFT TEXT (for continuation decisions only):\n" + next_head)
    if draft_text:
        parts.append("DRAFT OCR TEXT OF THIS PAGE (may contain errors; the image is authoritative):\n" + draft_text)
    else:
        parts.append("No draft text is available for this page; transcribe from the image.")
    parts.append("Return only the JSON object.")
    return "\n\n".join(parts)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_model_json(text: str) -> dict:
    """Tolerant JSON extraction: strips code fences and leading/trailing prose."""
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start : end + 1])
        raise


def normalize_result(data: dict) -> dict:
    """Coerce a parsed model result into the exact shape the pipeline stores."""
    label = data.get("label")
    if label is not None:
        label = str(label).strip() or None
    figs = []
    for f in data.get("figures") or []:
        if not isinstance(f, dict):
            continue
        bbox = f.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                bbox = [float(v) for v in bbox]
            except (TypeError, ValueError):
                bbox = None
        else:
            bbox = None
        figs.append({"id": str(f.get("id", "")), "alt": str(f.get("alt", "")), "bbox": bbox,
                     "caption": str(f.get("caption", ""))})
    return {
        "label": label,
        "skip": bool(data.get("skip", False)),
        "starts_mid_paragraph": bool(data.get("starts_mid_paragraph", False)),
        "ends_mid_paragraph": bool(data.get("ends_mid_paragraph", False)),
        "html": str(data.get("html", "") or ""),
        "figures": figs,
        "notes": str(data.get("notes", "") or ""),
    }
