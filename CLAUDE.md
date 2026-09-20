# Unscanner — guide for Claude (and people) changing this code

Purpose: turn scanned PDFs into accessible HTML/EPUB (WCAG 2.1 AA) while keeping printed page
numbers citable. Libraries adapt this tool to their own needs, often with Claude's help, so keep
changes small, plain, and covered by tests.

## Map

| File | Role |
|---|---|
| `src/unscanner/document.py` | on-disk state: `work/<doc>/doc.json`, one `Page` record per PDF page. The stored `workdir`/`source` are absolute; `Document.load` re-points them when the project folder was renamed or moved |
| `src/unscanner/pdf.py` | render pages, text layer, RapidOCR fallback, figure cropping |
| `src/unscanner/prompts.py` | **the transcription contract** (`GUIDELINES`), JSON schema, result normalization |
| `src/unscanner/backends/` | model backends: `anthropic_backend.py` (Claude API), `openai_compat.py` (Ollama/vLLM/etc.) |
| `src/unscanner/pipeline.py` | batch transcription, resumable, threaded |
| `src/unscanner/assemble.py` | per-page HTML -> one document: page markers, paragraph joins, heading normalization |
| `src/unscanner/epub.py` | EPUB 3 writer (TOC, page-list, accessibility metadata) |
| `src/unscanner/validate.py` | HTML checks, OCR coverage check, epubcheck runner |
| `src/unscanner/sanitize.py` | normalizes any HTML (editor or model) to the allowed semantic vocabulary |
| `src/unscanner/cli.py` | `unscanner` command line |
| `src/unscanner/mcp_server.py` | MCP tools for agents |
| `src/unscanner/webapp.py` + `web/` | local web UI (FastAPI + one plain HTML/JS page, no build step); mounts the MCP server at `/mcp` |
| `src/unscanner/keystore.py` | API keys in the OS credential store (optional `keyring` package); the web UI falls back to `work/settings.json` without one. `GET /api/settings` never returns a key |
| `src/unscanner/locate.py` | maps editor caret context to a word box on the scan (Follow / Mark in the UI); word boxes come from `pdf.cached_page_words` |
| `src/unscanner/diff.py` | word-level diff of the scan's word boxes against the editor's words (the Diff button): `missing` / `changed` boxes on the scan, `extra` / `changed` words in the editor. Display only: the editor highlights use the CSS Custom Highlight API, so nothing is added to the page HTML |
| `src/unscanner/project.py` | Export project / Import project: a document's state as one portable `<pdf name>.unscanner.json` (no paths, no PDF) to keep next to the PDF. Import needs the same PDF, sanitizes the HTML, refuses to overwrite an existing project without `replace`, and on a replace lifts every page version so open editors get a 409. UI buttons, `/api/documents/{id}/export`, `/api/projects/import`, CLI `export` / `import` |
| `src/unscanner/session.py` | `work/session.json`: what the UI shows (for `get_current_view`) and agent navigation requests (`show_page`) |

The program was called `remediate` until September 2026. Three things still accept the old name so
nobody loses work: `keystore.LEGACY_SERVICE` (saved API keys), `project.LEGACY_FORMATS` (exported
project files), and the `remediate.*` to `unscanner.*` localStorage move at the top of `web/app.js`
(unsaved drafts). "Remediate" as a verb (the `remediate_document` MCP prompt, prose) is the
accessibility term and stays.

## Invariants — do not break

1. Every non-skipped page contributes exactly one `role="doc-pagebreak"` marker whose id is
   `pg-<label>`; when a paragraph spans pages the marker sits inline inside the joined paragraph.
2. Output HTML only uses the vocabulary in `sanitize.ALLOWED_TAGS`; no inline styles, and only the
   classes in `sanitize.ALLOWED_CLASSES` (figure placement, `align-center`/`align-right` on headings,
   paragraphs and table cells, `align-left` on a `<th>`, `figure-description`, and `small-caps` on a `<span>` only, with the text in normal case). Everything the editor or a model produces goes through
   `sanitize_fragment` before it is stored.
3. Exactly one `<h1>` in the assembled document, no skipped heading levels.
4. Every `<img>` has an `alt` attribute.
5. `work/<doc>/doc.json` is the single source of truth; CLI, MCP server and web UI all read/write it
   through `Document`. Never store state anywhere else.
6. The EPUB must pass epubcheck with zero errors (`unscanner validate` runs it when Java is present).
7. Every stored page change goes through `pipeline.apply_result`, which bumps `page.version` and sets
   `page.changed_by` ("editor" for a person, "claude" for MCP `set_page`, a model id for batch runs). Status `done` means a person approved the page in the UI; a plain Save keeps it `needs_review`.
   The UI sends the version it loaded on save and the server answers 409 on a mismatch; never bypass
   this, it is what lets a person and Claude edit the same document at the same time.

## Where to make common changes

- House style for headings, footnotes, figures, what to drop: edit `GUIDELINES` in `prompts.py`
  (used by both backends and returned by the MCP `get_guidelines` tool).
- Who is doing the work and why (the institution, the legal basis): `CONTEXT` at the top of
  `prompts.py`. It is there to stop copyright false positives; keep it truthful for your institution.
- Model refusals: a backend raises `RefusalError` (not a plain `BackendError`) when a model declines
  a page; `pipeline.transcribe_pages(fallback=...)` retries such pages once on a second backend and
  marks them `needs_review`. Settings: `fallback_backend`, `send_title`; CLI `--fallback`, `--no-title`.
- Different model or endpoint: `backends/__init__.py` `make_backend`, or the UI Settings dialog
  (`work/settings.json`).
- Page fails with "output truncated": nearly always a local model stuck repeating itself, not a long
  page (a dense table page is ~2000 tokens). `openai_compat.py` retries such a page at
  `TRUNCATION_RETRY_TEMPERATURES`. The budget is `openai_max_tokens` (UI Settings, CLI `--max-tokens`,
  default 8000); raising it makes loops slower and gateways time out (504).
- A table the transcriber ran together as text: the editor's +Table button (select the text, click, drag
  a box over the table on the scan). `POST /api/documents/{id}/pages/{n}/table` sends only that crop to
  the configured model with `TABLE_PROMPT` from `prompts.py` (keep its table rules in step with
  `GUIDELINES`) and returns a sanitized `<table>`; nothing is stored until the person saves the page.
- A numbered list that runs over a page break: the new page's list is `<ol start="N">` (the editor's
  `n.` button; the sanitizer keeps only `start` and `type` on an `<ol>`). `assemble.py` makes one list
  of the two: `_carry_list_items` when the break falls inside an item (the continuation flags are set,
  so the item is joined first), `merge_continued_list` when it falls between items and `start` is the
  previous list's next number. Numbers that do not line up leave two lists and a build warning.
- Output look: `CSS` in `assemble.py` (shared by HTML and EPUB). Below it, `BOOK_CSS` (book paragraphs:
  no space between them, first-line indent only after another paragraph) and `JUSTIFY_CSS` are added
  per format by the export settings.
- Export settings: `EXPORT_DEFAULTS` in `assemble.py`, six switches `<html|epub>_<indent|justify|page_numbers>`
  plus `html_embed_images` and `html_bookmarks` in `work/settings.json` (the Export section of the UI Settings dialog). `cli._build` reads them for
  every build (UI, CLI, MCP) and hands each writer an `ExportStyle`. Page numbers off is the one allowed
  exception to invariant 1: `without_page_markers` takes the markers out of a copy for that format only.
  `html_embed_images` (on by default) makes the HTML (`<title>.html`, named like the EPUB; `cli.built_html` finds it) one self-contained file: `with_embedded_images`
  puts each figure in a copy of `<main>` as a `data:` URI, so the file opens with a double-click or uploads to
  an LMS without its `figures/` folder. The folder is still written (the EPUB and the UI read it), and stored
  page HTML never holds a `data:` URI.
  `html_bookmarks` (on by default) puts `bookmarks_nav`'s collapsed heading outline at the top of the HTML:
  the same nesting as the sidebar's Headings tab, each heading a link to its place in the text, branches that
  fold away as nested `<details>` so the file still needs no script. The `<h1>` is left out (it is the title),
  and a document with no other heading gets no nav. An EPUB has its own TOC, so the switch is HTML-only.
  `images_grayscale` and `images_jpeg` (both off by default) make the figure files smaller: `image_options`
  hands `assemble` an `ImageOptions`, and `resolve_figures` re-encodes each crop with `pdf.compress_image`
  (Pillow, `JPEG_QUALITY`). The figures are cropped once per build, so these hold for both formats; a JPEG
  build names its files `.jpg` and `image_mime` gives the type to the EPUB manifest, the data URIs and the
  figure route.
- Words broken over a page edge: `knit_word` in `assemble.py` (called by `merge_continuation`) drops an
  end-of-line hyphen and keeps a true one, going by how the rest of the document spells the word
  (`document_words`). A page edge inside a hyphenated word joins the paragraphs even when the
  continuation flags are not set (`word_broken`).
- The sidebar's Headings tab (a navigation pane like Word's): the outline comes from `assemble.outline`,
  served by `GET /api/documents/{id}/outline`, and the UI refetches it whenever a page's version changes.
  Levels are the ones written on the page, not the normalized ones a build produces, so the list matches
  what the editor shows; skipped pages contribute nothing. `app.js` nests the flat list into a tree and
  branches collapse: which are closed is kept per document in `unscanner.outlineCollapsed`, keyed by the
  heading's page and its place on that page, and a branch the page being shown sits in is reopened.
- Several pages at once: in the sidebar's Pages tab, Ctrl+click and Shift+click pick pages (`state.picked` in
  `app.js`) and a right-click opens `#page-menu`: Approve, Needs review, Skip, Don't skip.
  `POST /api/documents/{id}/pages/bulk` changes only the skip flag and the status, through `apply_result`, so
  every version is bumped (invariant 7). Skip and Don't skip never change the status (people skip pages just to
  build part of a document, and the record of what is approved must survive that); a page with no HTML cannot
  be approved and stays `pending`. The UI moves a draft made from the replaced version on to the new one, so unsaved edits still save.
- Which pages the Pages tab lists: the `#filter-pages` dropdown (All pages, Needs work, Has figures, Has tables).
  A filter is one predicate in `PAGE_FILTERS` in `app.js`; the page list, Approve & next and the page arrows all
  go by it, and the page being shown always stays listed. `has_figures` / `has_tables` come from `page_view` in
  `webapp.py`. The choice is kept in `unscanner.pageFilter` (the old checkbox's `unscanner.filterReview` is read once).
- New validation rule: add to `validate_html` in `validate.py` and cover it in `tests/`.
- New UI feature: `web/app.js` talks only to `/api/...` routes in `webapp.py`. Keep it vanilla JS.

## Running

```bash
pip install -e .[dev]
python -m pytest -q          # synthetic PDF, mocked HTTP; no API key needed
python -m unscanner.cli ui   # web UI at http://127.0.0.1:8765
python -m unscanner.cli serve   # MCP server on stdio (see .mcp.json)
```

Sample scans live in `pdfs/` (not committed). `work/` and `out/` are generated.

## Conventions

- Python 3.11+, type hints, no framework beyond FastAPI for the UI. Prefer the standard library.
- Anthropic SDK 1.x: uses `httpx2`; tests mock with `httpx2.MockTransport`.
- MCP SDK 2.x: `MCPServer` (not FastMCP); tool functions returning `dict[str, Any]` give structured results.
- Add or update a test for every behaviour change; tests must stay runnable offline.
