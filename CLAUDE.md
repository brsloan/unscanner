# Unscanner — guide for Claude (and people) changing this code

Purpose: turn scanned PDFs into accessible HTML/EPUB (WCAG 2.1 AA) while keeping printed page
numbers citable. Libraries adapt this tool to their own needs, often with Claude's help, so keep
changes small, plain, and covered by tests.

## Map

| File | Role |
|---|---|
| `src/unscanner/document.py` | on-disk state: `work/<doc>/doc.json`, one `Page` record per PDF page. The stored `workdir`/`source` are absolute; `Document.load` re-points them when the project folder was renamed or moved. A project is identified by its PDF's content, not its name: `fingerprint` (size plus a hash of the first and last megabyte) is stored in the document, the work folder is `doc_id_for` (`<slug>-<8 hex>`), and `find_project` finds the folder for a fingerprint. Folders made before fingerprints (named after the PDF alone, `fingerprint == ""`) are adopted by name on their next open, so nothing is renamed |
| `src/unscanner/pdf.py` | render pages, text layer, RapidOCR fallback, figure cropping. `new_document` opens a PDF where it is (no copy) and reopens its project by fingerprint, updating `source` to the file just chosen. A new project takes its title and author from the PDF's metadata unless `usable_title` / `usable_author` judge them machine-made ("Microsoft Word - x.doc", ids, placeholders, service accounts); the title then falls back to the file name. `relink_source` (Locate) and `remove_project` (Remove; deletes an upload's `_inbox` copy, never a PDF elsewhere). Page renders and word boxes are a cache: `cache_dir` is `<workdir>/pages`, or `<cache_root>/<doc_id>` when `cache_root` is set (the installed app sets it to `AppData\Local\Unscanner\cache`, out of OneDrive's reach, and a `pages/` left in the work folder is dropped); `trim_cache` keeps all caches under `CACHE_MAX_BYTES`, least recently used first, and runs at UI start and on every open |
| `src/unscanner/prompts.py` | **the transcription contract** (`GUIDELINES` = `CONTEXT` + `TASK` + `RULES`), JSON schema, result normalization; a library's own texts for the editable parts in `work/prompts/` |
| `src/unscanner/backends/` | model backends: `anthropic_backend.py` (Claude API), `openai_compat.py` (Ollama/vLLM/etc.) |
| `src/unscanner/pipeline.py` | batch transcription, resumable, threaded |
| `src/unscanner/assemble.py` | per-page HTML -> one document: page markers, paragraph joins, heading normalization |
| `src/unscanner/epub.py` | EPUB 3 writer (TOC, page-list, accessibility metadata) |
| `src/unscanner/validate.py` | HTML checks, OCR coverage check, epubcheck runner |
| `src/unscanner/sanitize.py` | normalizes any HTML (editor or model) to the allowed semantic vocabulary |
| `src/unscanner/cli.py` | `unscanner` command line |
| `src/unscanner/mcp_server.py` | MCP tools for agents |
| `src/unscanner/webapp.py` + `web/` | local web UI (FastAPI + one plain HTML/JS page, no build step); mounts the MCP server at `/mcp`. `serve` shows it in a pywebview window when the optional `window` extra is installed (the server then runs on a thread; closing the window stops it), else in the browser (see `desktop.py`). In the window, files are chosen with the system's Open dialog (`POST /api/pick-file`, `desktop.pick_file`; `GET /api/app` says whether there is a window) and opened in place; a browser tab uploads into `work/_inbox` instead (`.window-only` / `.browser-only` in the markup). Open PDF has no form: it goes straight to the file chooser and, when the open made a new project (`created` in the response), opens Properties beside the scan for the title, author and language. The top bar's File menu (`#btn-file`, a `role="menu"` popup positioned `fixed` so the scrolling bar cannot clip it) holds Open PDF, Save, Recent projects (a submenu of every project, most recently opened first, `renderRecent` in `app.js`; it replaced the document dropdown), Manage projects…, Export project, Import project…, Settings… and Help… (`#dlg-help`, static text for a non-technical user); Open PDF and Save are also toolbar buttons. The open document's title goes in the title bar, not on the page: `document.title` for a browser tab, and `PUT /api/app/title` (`desktop.set_title`) renames the pywebview window. The Manage projects… dialog lists every project, most recently opened first, flags one whose PDF is gone (`source_found`), and offers Locate (`POST .../source`), Remove (`DELETE /api/documents/{id}`, with an export first if asked) and "Remove all whose PDF is missing" |
| `src/unscanner/desktop.py` | the UI as a desktop app: the pywebview window (`window_engine` checks for WebView2 first, since without it pywebview silently uses Internet Explorer's engine; the window's localStorage lives in `work/.webview/`) and no console: `start-unscanner.bat` runs `ui --no-console`, which, when a window can open and the port is free, starts the same command with pythonw.exe and exits. That copy logs to `work/unscanner.log`, shows fatal errors in a message box, and if its window fails reopens itself in a console with `--browser`, since a browser tab cannot stop the app |
| `src/unscanner/keystore.py` | API keys in the OS credential store (optional `keyring` package); the web UI falls back to `work/settings.json` without one. `GET /api/settings` never returns a key |
| `src/unscanner/locate.py` | maps editor caret context to a word box on the scan (Follow / Mark in the UI); word boxes come from `pdf.cached_page_words` |
| `src/unscanner/diff.py` | word-level diff of the scan's word boxes against the editor's words (the Diff button): `missing` / `changed` boxes on the scan, `extra` / `changed` words in the editor. Display only: the editor highlights use the CSS Custom Highlight API, so nothing is added to the page HTML |
| `src/unscanner/project.py` | Export project / Import project: a document's state as one portable `<pdf name>.unscanner.json` (no paths, no PDF) to keep next to the PDF. The file carries the PDF's fingerprint: import refuses another PDF (older files without one are checked by page count only), finds the existing project by it, sanitizes the HTML, refuses to overwrite an existing project without `replace`, and on a replace lifts every page version so open editors get a 409. UI buttons, `/api/documents/{id}/export`, `/api/projects/import` (the project as an upload or, from the window, a `project_path`), CLI `export` / `import` |
| `src/unscanner/session.py` | `work/session.json`: what the UI shows (for `get_current_view`) and agent navigation requests (`show_page`) |
| `src/unscanner/app.py` | entry points of the installed app: `Unscanner.exe` (`gui`: `ui`, no console) and `unscanner-cli.exe` (`console`: the CLI; not `unscanner.exe`, file names ignore case). Both default `--work`/`--out` to `Documents\Unscanner`; the window's browser data and the page cache (`pdf.cache_root`) go to `AppData\Local\Unscanner` (not synced by OneDrive). `desktop._command` reopens with these programs when `sys.frozen` |
| `scripts/build_installer.py` + `scripts/installer/` | the Windows installer: a clean build venv in `build/installer/venv`, PyInstaller (`unscanner.spec`: two programs sharing one `_internal`), the C++ runtime and epubcheck copied in, Inno Setup (`unscanner.iss`: per-user, `PrivilegesRequired=lowest`; never change its `AppId`). The icon is drawn from `favicon.svg`'s shapes (`ICON_SHAPES`), so keep the two in step |
| `.github/workflows/release.yml` | a pushed tag `v<version>` runs the tests, builds the portable zip and the installer on a Windows runner (epubcheck downloaded, Inno Setup from Chocolatey) and publishes both as a GitHub release; the tag must match `pyproject.toml` and `unscanner.__version__`. Run by hand, it only keeps the files as run artifacts |
| `scripts/build_portable.py` | the portable Windows zip: python.org's embeddable Python with the dependencies in `python/`, the source as plain files in `src/`, and the same `start-unscanner.bat` (it uses `python\python.exe` when that exists). Launchers run `python.exe -s` because the `._pth` file does not keep out the user's own site-packages |

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

- House style for headings, footnotes, figures, what to drop: `RULES` in `prompts.py`. Who is doing the
  work and why (the institution, the legal basis): `CONTEXT`; it is there to stop copyright false
  positives, so keep it truthful. Both, and `TABLE_RULES`, are only the built-in defaults: a library changes
  them in the UI (Settings > Edit prompts, `GET`/`PUT /api/prompts`), which writes `work/prompts/context.txt`,
  `rules.txt`, `table-rules.txt`. `guidelines(work_root)` and `build_table_prompt(text, work_root)` put the
  pieces together; `pipeline.transcribe_pages` reads them once per run from the document's work folder and
  hands them to `Backend.transcribe(..., system=)`, and the MCP `get_guidelines` tool returns the same text.
  `TASK` (the JSON output contract) is not editable: `normalize_result` and `OUTPUT_SCHEMA` depend on it.
  `based-on.json` records which default each file was edited from, so the dialog can say when an update
  improved a default the library replaced. Changing a default in code changes it for every library that has
  not replaced it.
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
- New UI feature: `web/app.js` talks only to `/api/...` routes in `webapp.py`. Keep it vanilla JS. Describe the
  feature in the Help dialog (`#dlg-help` in `index.html`) in plain words, and change its section when a feature changes.

## Running

```bash
pip install -e .[dev]
python -m pytest -q          # synthetic PDF, mocked HTTP; no API key needed
python -m unscanner.cli ui   # web UI at http://127.0.0.1:8765
python -m unscanner.cli serve   # MCP server on stdio (see .mcp.json)
python scripts/build_portable.py   # dist/unscanner-<version>-win64.zip, needs Windows and network
python scripts/build_installer.py  # dist/Unscanner-Setup-<version>.exe, also needs Inno Setup 6
```

Sample scans live in `pdfs/` (not committed). `work/` and `out/` are generated.

## Conventions

- Python 3.11+, type hints, no framework beyond FastAPI for the UI. Prefer the standard library.
- Anthropic SDK 1.x: uses `httpx2`; tests mock with `httpx2.MockTransport`.
- MCP SDK 2.x: `MCPServer` (not FastMCP); tool functions returning `dict[str, Any]` give structured results.
- Add or update a test for every behaviour change; tests must stay runnable offline.
