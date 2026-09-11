"""MCP server exposing the remediation workflow to an agent (Claude Code, Claude Desktop, or any MCP client).

Two ways an agent can do the work:
  1. Look and write: get_page returns the page image + draft text; the agent writes the HTML itself and
     stores it with set_page. No API key needed; the agent's own model does the reading.
  2. Delegate: transcribe_pages runs the configured backend (Claude API or a local OpenAI-compatible
     model) over many pages at once; the agent reviews flagged pages afterwards.

Environment: REMEDIATE_WORK_DIR (default ./work), REMEDIATE_OUT_DIR (default ./out), plus the
REMEDIATE_BACKEND / REMEDIATE_MODEL / REMEDIATE_OPENAI_BASE_URL variables used by the backends.
"""

from __future__ import annotations

import json
import os
from typing import Any
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

from .document import Document, parse_page_range, slugify
from .pdf import cached_page_png, new_document
from .pipeline import apply_result, ensure_draft_text
from .prompts import GUIDELINES, normalize_result

server = MCPServer(
    "remediate",
    instructions=(
        "Converts scanned PDFs into accessible HTML/EPUB (WCAG 2.1 AA) while keeping printed page numbers "
        "citable. Typical flow: open_document -> (get_page + set_page for each page, or transcribe_pages) "
        "-> build -> validate -> fix flagged pages -> build again. Call get_guidelines once before writing "
        "page HTML yourself."
    ),
)


def _work_root() -> Path:
    return Path(os.environ.get("REMEDIATE_WORK_DIR", "work")).resolve()


def _out_root() -> Path:
    return Path(os.environ.get("REMEDIATE_OUT_DIR", "out")).resolve()


def _load(doc_id: str) -> Document:
    wd = _work_root() / doc_id
    if not Document.exists(wd):
        raise ValueError(f"unknown document {doc_id!r}; call open_document or list_documents")
    return Document.load(wd)


@server.tool()
def open_document(pdf_path: str, title: str = "", author: str = "", language: str = "en") -> dict[str, Any]:
    """Open a PDF for remediation (creates or reopens its work directory) and return its status summary.

    pdf_path: path to the scanned PDF. title/author: override the metadata used for the output
    document (defaults come from the PDF metadata or file name). language: BCP-47 code, e.g. "en".
    Returns doc_id (use it for every other tool), page_count and per-page status counts.
    """
    doc = new_document(pdf_path, _work_root(), title=title, author=author, language=language)
    return {"doc_id": Path(doc.workdir).name, **doc.summary()}


@server.tool()
def list_documents() -> list[dict[str, Any]]:
    """List documents already opened in the work directory with their status summaries."""
    out = []
    for wd in sorted(_work_root().glob("*/")):
        if Document.exists(wd):
            d = Document.load(wd)
            out.append({"doc_id": wd.name, **d.summary()})
    return out


@server.tool()
def get_status(doc_id: str) -> dict[str, Any]:
    """Status of every page: index, printed label, status (pending/done/needs_review/error), notes."""
    doc = _load(doc_id)
    return {
        **doc.summary(),
        "pages": [{"index": p.index, "label": p.label, "status": p.status, "skip": p.skip,
                   "words": len((p.html or "").split()), "notes": p.notes} for p in doc.pages],
    }


@server.tool()
def get_guidelines() -> str:
    """The transcription contract (HTML rules, page-number labels, footnotes, figures, continuation flags).
    Read this once before writing page HTML with set_page so your output matches what the assembler expects."""
    return GUIDELINES


@server.tool()
def get_page(doc_id: str, page: int, include_image: bool = True, include_draft: bool = True):
    """Return one page for transcription or review: the rendered page image, the draft OCR text, the
    neighbouring pages' draft text edges (for continuation decisions), and any HTML already stored.

    page is the 1-based PDF page index. After reading the image, call set_page with the HTML.
    """
    doc = _load(doc_id)
    p = doc.page(page)
    ensure_draft_text(doc, [page])
    prev_tail = doc.page(page - 1).draft_text[-300:] if page > 1 else ""
    next_head = doc.page(page + 1).draft_text[:200] if page < len(doc.pages) else ""
    info = {
        "doc_id": doc_id, "page": page, "of": len(doc.pages), "label": p.label, "status": p.status,
        "skip": p.skip, "starts_mid_paragraph": p.starts_mid_paragraph, "ends_mid_paragraph": p.ends_mid_paragraph,
        "notes": p.notes, "draft_source": p.draft_source,
        "previous_page_ends_with": prev_tail, "next_page_starts_with": next_head,
    }
    if include_draft:
        info["draft_text"] = p.draft_text
    if p.html:
        info["current_html"] = p.html
    parts: list = [json.dumps(info, ensure_ascii=False, indent=1)]
    if include_image:
        parts.append(Image(path=str(cached_page_png(doc, page))))
    return parts


@server.tool()
def set_page(doc_id: str, page: int, html: str, label: str | None = None, starts_mid_paragraph: bool = False,
             ends_mid_paragraph: bool = False, skip: bool = False, notes: str = "",
             figures: list[dict] | None = None) -> dict[str, Any]:
    """Store the transcription of one page (written by you, following get_guidelines).

    html: body fragment for the page. label: printed page number as shown on the page (null if none).
    starts_mid_paragraph / ends_mid_paragraph: whether the page's first/last paragraph continues from
    or onto the neighbouring page (the assembler joins them and places the page marker inline).
    skip: true for blank or content-free pages. notes: anything a human should check (marks the page
    needs_review). figures: [{"id","alt","bbox":[x0,y0,x1,y1] in 0-1000 page coords,"caption"}] for
    each <img src="fig:ID"> in the html; bbox lets the builder crop the image out of the scan.
    """
    doc = _load(doc_id)
    result = normalize_result({"label": label, "skip": skip, "starts_mid_paragraph": starts_mid_paragraph,
                               "ends_mid_paragraph": ends_mid_paragraph, "html": html,
                               "figures": figures or [], "notes": notes})
    apply_result(doc.page(page), result, model="agent")
    doc.save()
    p = doc.page(page)
    return {"page": page, "status": p.status, "label": p.label, "words": len(p.html.split()),
            "pending_pages": doc.summary()["pending_pages"][:20]}


@server.tool()
def transcribe_pages(doc_id: str, pages: str = "all", backend: str | None = None, model: str | None = None,
                     force: bool = False, workers: int = 4) -> dict[str, Any]:
    """Run the configured model backend over pages (batch). pages: "all", "3", "1-5,9".

    backend: "anthropic" (Claude API; needs ANTHROPIC_API_KEY) or "openai" (any OpenAI-compatible
    endpoint such as Ollama/vLLM; set REMEDIATE_OPENAI_BASE_URL). Defaults come from REMEDIATE_BACKEND /
    REMEDIATE_MODEL. Pages already done are skipped unless force=true. Returns counts and token usage;
    check get_status afterwards for needs_review pages.
    """
    from .backends import make_backend
    from .pipeline import transcribe_pages as _run

    doc = _load(doc_id)
    be = make_backend(backend, model)
    idx = parse_page_range(pages, len(doc.pages))
    return _run(doc, be, idx, force=force, workers=workers)


@server.tool()
def build(doc_id: str, epub: bool = True) -> dict[str, Any]:
    """Assemble all transcribed pages into out/<doc>/index.html (and an EPUB 3 with page-list navigation).
    Returns output paths and assembler warnings (e.g. pages not yet transcribed, duplicate ids)."""
    from .cli import _build

    doc = _load(doc_id)
    return _build(doc, str(_out_root()), epub=epub)


@server.tool()
def validate(doc_id: str, epubcheck: bool = True) -> dict[str, Any]:
    """Run accessibility and completeness checks on the last build: HTML structure (lang, title,
    headings, alt text, tables, links, ids, page markers), per-page word-count coverage against the OCR
    draft (catches skipped or invented text), and W3C epubcheck when Java is available.
    Returns {counts, issues:[{severity, code, message, location}]}. Fix flagged pages with set_page."""
    from .cli import _validate

    doc = _load(doc_id)
    return _validate(doc, str(_out_root()), run_epubcheck=epubcheck)


@server.tool()
def get_output_html(doc_id: str, max_chars: int = 200000) -> str:
    """Return the assembled HTML from the last build (truncated to max_chars) for review."""
    p = _out_root() / slugify(Path(_load(doc_id).source).stem) / "index.html"
    if not p.exists():
        raise ValueError("no build yet; call build first")
    return p.read_text(encoding="utf-8")[:max_chars]


@server.prompt()
def remediate_document(pdf_path: str) -> str:
    """Step-by-step instructions for remediating one PDF with this server."""
    return (
        f"Remediate the scanned PDF at {pdf_path} into accessible HTML and EPUB.\n"
        "1. Call open_document, then get_guidelines.\n"
        "2. For each page in order: get_page, read the image carefully, and set_page with faithful "
        "semantic HTML, the printed page label, and the continuation flags. If a model backend is "
        "configured you may instead call transcribe_pages for the bulk and only hand-check pages "
        "listed as needs_review.\n"
        "3. build, then validate. Fix every error and look at each warning; re-run build.\n"
        "4. Report the output paths and anything a human should still check."
    )


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
