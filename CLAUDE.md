# remediate — guide for Claude (and people) changing this code

Purpose: turn scanned PDFs into accessible HTML/EPUB (WCAG 2.1 AA) while keeping printed page
numbers citable. Libraries adapt this tool to their own needs, often with Claude's help, so keep
changes small, plain, and covered by tests.

## Map

| File | Role |
|---|---|
| `src/remediate/document.py` | on-disk state: `work/<doc>/doc.json`, one `Page` record per PDF page |
| `src/remediate/pdf.py` | render pages, text layer, RapidOCR fallback, figure cropping |
| `src/remediate/prompts.py` | **the transcription contract** (`GUIDELINES`), JSON schema, result normalization |
| `src/remediate/backends/` | model backends: `anthropic_backend.py` (Claude API), `openai_compat.py` (Ollama/vLLM/etc.) |
| `src/remediate/pipeline.py` | batch transcription, resumable, threaded |
| `src/remediate/assemble.py` | per-page HTML -> one document: page markers, paragraph joins, heading normalization |
| `src/remediate/epub.py` | EPUB 3 writer (TOC, page-list, accessibility metadata) |
| `src/remediate/validate.py` | HTML checks, OCR coverage check, epubcheck runner |
| `src/remediate/sanitize.py` | normalizes any HTML (editor or model) to the allowed semantic vocabulary |
| `src/remediate/cli.py` | `remediate` command line |
| `src/remediate/mcp_server.py` | MCP tools for agents |
| `src/remediate/webapp.py` + `web/` | local web UI (FastAPI + one plain HTML/JS page, no build step); mounts the MCP server at `/mcp` |
| `src/remediate/session.py` | `work/session.json`: what the UI shows (for `get_current_view`) and agent navigation requests (`show_page`) |

## Invariants — do not break

1. Every non-skipped page contributes exactly one `role="doc-pagebreak"` marker whose id is
   `pg-<label>`; when a paragraph spans pages the marker sits inline inside the joined paragraph.
2. Output HTML only uses the vocabulary in `sanitize.ALLOWED_TAGS`; no inline styles or classes
   (except `figure-description`). Everything the editor or a model produces goes through
   `sanitize_fragment` before it is stored.
3. Exactly one `<h1>` in the assembled document, no skipped heading levels.
4. Every `<img>` has an `alt` attribute.
5. `work/<doc>/doc.json` is the single source of truth; CLI, MCP server and web UI all read/write it
   through `Document`. Never store state anywhere else.
6. The EPUB must pass epubcheck with zero errors (`remediate validate` runs it when Java is present).
7. Every stored page change goes through `pipeline.apply_result`, which bumps `page.version` and sets
   `page.changed_by` ("editor" for a person, "claude" for MCP `set_page`, a model id for batch runs).
   The UI sends the version it loaded on save and the server answers 409 on a mismatch; never bypass
   this, it is what lets a person and Claude edit the same document at the same time.

## Where to make common changes

- House style for headings, footnotes, figures, what to drop: edit `GUIDELINES` in `prompts.py`
  (used by both backends and returned by the MCP `get_guidelines` tool).
- Different model or endpoint: `backends/__init__.py` `make_backend`, or the UI Settings dialog
  (`work/settings.json`).
- Output look: `CSS` in `assemble.py` (shared by HTML and EPUB).
- New validation rule: add to `validate_html` in `validate.py` and cover it in `tests/`.
- New UI feature: `web/app.js` talks only to `/api/...` routes in `webapp.py`. Keep it vanilla JS.

## Running

```bash
pip install -e .[dev]
python -m pytest -q          # synthetic PDF, mocked HTTP; no API key needed
python -m remediate.cli ui   # web UI at http://127.0.0.1:8765
python -m remediate.cli serve   # MCP server on stdio (see .mcp.json)
```

Sample scans live in `pdfs/` (not committed). `work/` and `out/` are generated.

## Conventions

- Python 3.11+, type hints, no framework beyond FastAPI for the UI. Prefer the standard library.
- Anthropic SDK 1.x: uses `httpx2`; tests mock with `httpx2.MockTransport`.
- MCP SDK 2.x: `MCPServer` (not FastMCP); tool functions returning `dict[str, Any]` give structured results.
- Add or update a test for every behaviour change; tests must stay runnable offline.
