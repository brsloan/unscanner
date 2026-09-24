# Unscanner: scanned PDF to accessible HTML and EPUB

Turns scanned course readings (book chapters, articles) into semantic HTML and EPUB 3 that meet
WCAG 2.1 AA, with the printed page numbers preserved as navigable markers so students can still
cite by page. A vision language model reads each page image; the tool handles everything around
that: rendering, OCR drafts, assembly, page-break markers, EPUB packaging and validation.

It can be driven three ways:

| Mode | Who reads the page | Needs |
|---|---|---|
| **Agent with MCP** (Claude Code / Claude Desktop / any MCP client) | the agent's own model, via `get_page` + `set_page` | nothing beyond the agent |
| **Batch with Claude API** | `claude-opus-5` (or Sonnet/Haiku) | `ANTHROPIC_API_KEY` |
| **Batch with a local model** | any vision model behind an OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) | the endpoint URL |

## Install

```bash
pip install -e .
```

Python 3.11+. Optional for EPUB validation: Java 21 and [EPUBCheck](https://www.w3.org/publishing/epubcheck/)
unpacked into `tools/epubcheck-*/` (or set `EPUBCHECK_JAR` to point at `epubcheck.jar` elsewhere).

**For colleagues without Python (Windows):** `python scripts/build_portable.py` makes
`dist/unscanner-<version>-win64.zip` (about 165 MB, or 130 MB with `--no-epubcheck`). It holds its own
Python and every dependency. People unzip it anywhere and double-click `start-unscanner.bat`: nothing to
install and no admin rights needed. The source stays plain `.py` files a library can adapt. See
`PORTABLE.txt` in the zip.

**Before your first run, open Settings > Edit prompts in the web UI and rewrite "Who is doing the work
and why".** It tells the model who is doing the work and on what legal basis (a university library's
accessibility service, source held lawfully, use reviewed by counsel) so that faithful transcription is
not mistaken for a copyright problem. It is sent with every page. Those statements were true where this
tool was written; make them true for your institution, or remove what does not apply.

The same dialog holds the house style (headings, footnotes, tables, figures, what to drop) and the
+Table rules. Your versions are saved as text files in `work/prompts/` (`context.txt`, `rules.txt`,
`table-rules.txt`), which the CLI and the MCP server use too; you can also edit or share those files
directly. Delete one, or empty its box, to go back to the built-in text. The dialog tells you when an
update has improved a built-in text you replaced. The JSON output format the program reads is not
editable. The built-in texts are `CONTEXT`, `RULES` and `TABLE_RULES` in `src/unscanner/prompts.py`.

If the `unscanner` command is not on your PATH (pip's user Scripts folder often is not), use
`python -m unscanner.cli` in its place everywhere below.

## Command line

```bash
# one shot: open + transcribe + build + validate
unscanner run "pdfs/HIST 101 Harbor Towns.pdf" --title "Harbor Towns of the North (excerpt)" --author "Jane Q. Example"

# or step by step
unscanner open  pdfs/x.pdf --title "..." --author "..."
unscanner transcribe pdfs/x.pdf --pages 1-10 --backend anthropic --model claude-sonnet-5 --effort low
unscanner transcribe pdfs/x.pdf --backend openai --model qwen3.6:27b      # Ollama at localhost:11434
unscanner build pdfs/x.pdf            # out/<doc>/<title>.html + out/<doc>/<title>.epub
unscanner validate pdfs/x.pdf         # HTML checks + coverage check + epubcheck
unscanner status pdfs/x.pdf
unscanner page pdfs/x.pdf 7           # print the stored HTML for page 7
unscanner set-page pdfs/x.pdf 7 fixed.html --label 81   # replace a page by hand
```

State lives in `work/<doc>/doc.json` (one record per page: label, HTML, flags, notes, OCR draft,
token usage) plus cached page renders. Everything is resumable; re-running `transcribe` only
touches pages that are still `pending` or `error` unless `--force` is given.

Environment variables (CLI flags override them):

| Variable | Meaning |
|---|---|
| `UNSCANNER_BACKEND` | `anthropic` (default) or `openai` |
| `UNSCANNER_MODEL` | model id, e.g. `claude-opus-5`, `claude-sonnet-5`, `qwen3.6:27b` (the default for `openai`; the guidelines were tuned against it) |
| `UNSCANNER_OPENAI_BASE_URL` | e.g. `http://localhost:11434/v1` (Ollama), `http://gpu-box:8000/v1` (vLLM) |
| `UNSCANNER_OPENAI_API_KEY` | only if the endpoint requires one |
| `ANTHROPIC_API_KEY` | for the Claude backend |
| `UNSCANNER_WORK_DIR`, `UNSCANNER_OUT_DIR` | where the MCP server keeps state and writes output |

## Local web UI

```bash
python -m unscanner.cli ui          # opens http://127.0.0.1:8765 in its own window, or your browser
```

With the optional `pywebview` package (`pip install -e .[window]`; the portable zip has it) the UI opens
in a window of its own, and closing the window stops the app. On Windows this needs the Microsoft Edge
WebView2 Runtime, which Windows 11 includes. Started from `start-unscanner.bat` (`ui --no-console`) it
also runs without a console window: the console closes after a moment, messages go to
`work/unscanner.log`, and an error that stops the app appears in a message box. Without pywebview or
WebView2 it opens in your browser as before, the console stays, and closing it or Ctrl+C stops the app. `--browser` opens a browser tab anyway, and `--no-browser` only serves.
In the window, downloads (HTML, EPUB, Export project) ask where to save, and the HTML preview opens in
your browser. Unsaved drafts and layout are kept in `work/.webview/`, apart from the browser's copy, so
save your pages before switching between the two.

The UI is for the review step, which is where the human time goes. It shows the scanned page next
to an editor for that page's transcription, with the page list colour-coded by approval status (green approved,
amber needs review, grey not transcribed, red error) and review pages opened first.

- **Editor**: paragraphs, headings, lists, block quotes, strong/emphasis, superscript, alt text for a
  selected image, `lang` marking for foreign-language passages. There is no way to produce inline
  styles, fonts or layout tables: everything saved goes through the same sanitizer as model output
  and is reduced to the accessible vocabulary. An "HTML source" toggle gives a raw view for precise
  edits (tables, footnotes), and "OCR draft" shows the baseline text.
- **Figures**: click a figure in the editor to open the figure panel above the save bar: alt text
  (with an "AI autofill" button that sends the cropped image to the configured model and fills in a
  suggestion, or leaves it empty when the model judges the image decorative), alignment (left,
  center, right) and "wrap text" for left/right-aligned figures. Selecting a figure also shows its
  crop box on the scan with drag handles: move it or resize it to fix the model's bounds, and the
  editor preview updates live; "Draw crop" lets you draw a box for a figure that has none, and the
  "+Fig" toolbar button inserts a new figure at the caret for an image the model missed. Crop
  changes are kept when you Save. Placement is stored as the classes
  `align-left|center|right` and `wrap` on the `<figure>`; these are the only classes the sanitizer
  keeps, and the output CSS styles them in both HTML and EPUB. A heading or paragraph may also carry
  `align-center` (or `align-right`) when it was set that way in print.
- **Missed tables**: when the transcriber ran a table together as ordinary text, select that text in
  the editor, click **+Table**, and drag a box over the table on the scan. The configured model
  re-reads just that region as a `<table>`, which replaces the selection (with no selection it goes in
  after the caret's paragraph). Check it against the scan; Ctrl+Z takes it out again, and nothing is
  stored until you Save. Click +Table a second time to cancel the draw. When a table is the last
  thing on the page, arrow down (or right) from the end of its last cell starts a paragraph below it;
  arrow up (or left) from the start of its first cell does the same above a table that opens the page.
- **Editing and approval**: unsaved edits are kept per page in the browser, so you can move between
  pages freely and come back; pages with unsaved edits show a pencil in the page list. **Save** in the
  top bar (Ctrl+S) is document-level: it writes every page with unsaved edits without approving
  them. **Approve** (Ctrl+Enter) stores the current page and marks it approved, which is what the green dot
  in the page list means (amber: needs review, grey: not transcribed). Editing an approved page and
  saving puts it back to needs review. Alt+Left/Right moves between pages; "Approve & next" does both.
- **Page fields**: printed page number, starts/ends mid-paragraph flags, skip, notes.
- **Follow and Mark**: with Follow on, moving the caret through the editor scrolls the scan to keep
  the matching line in view; with Mark on, a red dot sits just left of the word the caret is on, and
  short red ticks on the four edges of the preview pane point at its row and column so the eye can
  find it without anything cluttering the text. The two toggles are independent. The link works both ways: clicking a word on the scan selects
  it in the editor and scrolls it into view. Matching uses the word boxes from
  the PDF text layer (or OCR boxes when there is none) and a few words of context around the caret,
  so it also works when the transcription differs slightly from the OCR.
- **Layout**: the scan opens scaled to fit; Fit and Fill buttons and a zoom slider sit next to the
  page arrows. Drag the borders between the page list, the scan and the editor to resize them
  (double-click a border to reset; the widths are remembered). Status and model notes appear in a
  one-line bar at the bottom with an arrow to expand longer messages.
- **Transcribe…** runs the configured model backend over a page range as a background job with
  live progress; **Build** writes the HTML and EPUB and shows download links plus a preview;
  **Validate** shows the checks in a panel with links that jump to the offending page.
- **Open PDF…** takes a path on this computer or an upload (copied into `work/_inbox/`).
- **Settings** stores the backend choice, model, effort and endpoint URL in `work/settings.json`;
  **Edit prompts…** in it changes what the model is told (see Install).
  API keys go to the operating system's credential store (Windows Credential Manager, macOS
  Keychain, GNOME Keyring/KWallet) when the optional `keyring` package is installed
  (`pip install -e .[keyring]`); a key already in `settings.json` is moved there the next time the
  UI starts. Without the package, or on a machine with no credential store (a headless server),
  keys stay in `work/settings.json` in plain text, and the Settings dialog says so. Either way a
  saved key is never sent back to the browser. The CLI and the stdio MCP server read keys from
  environment variables only.

The UI is one FastAPI file (`webapp.py`) and one plain HTML/JS/CSS page under `web/`, with no build
step, so a library can adapt it with Claude's help; see `CLAUDE.md`.

### Working on a document together with Claude

Keep the UI open and talk to Claude (Desktop or Code) in another window. Claude connects to the same
document state through the MCP server, which is also mounted inside the UI at
`http://127.0.0.1:8765/mcp` while `unscanner ui` is running (the stdio server in `.mcp.json`
works too; both share `work/session.json`).

- The UI reports the page you are on, and any text you have selected, so "this table is wrong" or
  "retry these equations" needs no page numbers. Claude calls `get_current_view` and sees the same
  page image and HTML you see.
- When Claude saves a page with `set_page`, your editor reloads within two seconds and shows a
  banner "updated by claude". The page list marks it too. Click Approve to confirm it.
- If you have unsaved edits when Claude changes the page, the banner offers to reload or keep yours.
  The same protection works the other way: a save based on a stale copy is refused and you choose.
- Claude can call `show_page` to bring a page up in your window ("look at page 12").
- Claude can rerun the model on a range with extra guidance (`transcribe_pages` with
  `instructions`), and the Transcribe dialog in the UI has the same "extra instructions" box.

For Claude Desktop, add the running UI as a remote MCP server with URL `http://127.0.0.1:8765/mcp`,
or use the stdio configuration shown below. For Claude Code, `.mcp.json` in this folder is picked up
automatically, or run `claude mcp add --transport http unscanner http://127.0.0.1:8765/mcp`.

## MCP server

Start with `unscanner serve` (stdio). `.mcp.json` in this folder already registers it for Claude Code;
open this folder in Claude Code and ask, for example:

> Remediate `pdfs/HIST101-HarborTowns.pdf` into accessible HTML and EPUB. Use the unscanner tools.

For Claude Desktop add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "unscanner": {
      "command": "python",
      "args": ["-m", "unscanner.cli", "--work", "C:/path/to/pdf-to-html/work", "--out", "C:/path/to/pdf-to-html/out", "serve"]
    }
  }
}
```

Tools:

| Tool | Purpose |
|---|---|
| `open_document(pdf_path, title, author, language)` | create/reopen the work directory, returns `doc_id` |
| `set_properties(doc_id, title, author, language)` | change the output metadata (omitted fields are kept); same as Properties… in the web UI |
| `list_documents()`, `get_status(doc_id)` | progress per page |
| `get_guidelines()` | the transcription contract (same rules the batch backends use) |
| `get_page(doc_id, page)` | page image + OCR draft + neighbours' edges + any stored HTML |
| `set_page(doc_id, page, html, label, starts_mid_paragraph, ends_mid_paragraph, skip, notes, figures)` | store a page |
| `transcribe_pages(doc_id, pages, backend, model, force)` | batch-run a model backend over pages |
| `build(doc_id)` | write `<title>.html` and the EPUB |
| `validate(doc_id)` | structural HTML checks, per-page coverage check, epubcheck |
| `get_output_html(doc_id)` | read back the assembled HTML |

The intended agent loop is: open, guidelines, then for every page `get_page` and `set_page` (or
`transcribe_pages` for the bulk and hand-check the `needs_review` pages), then `build`, `validate`,
fix, `build` again. The `remediate_document` prompt on the server spells this out for the agent.

Local models (Llama, Qwen) are unreliable at driving that multi-step tool loop themselves. Use them
through the batch backend instead, where the server does the orchestration and the model only has
to answer "here is a page image and a draft, return corrected HTML as JSON".

## What the output looks like

- One `<title>.html` per document: `<main>` with h1..h6, p, lists, tables with `<th scope>`,
  `<blockquote>`, `<figure>` with alt text (cropped out of the scan when the model gives a
  bounding box), footnotes as `<aside role="doc-footnote">` linked from `role="doc-noteref"`.
- Every scanned page starts with a marker: `<div role="doc-pagebreak" id="pg-81" aria-label="Page 81">81</div>`.
  When a paragraph runs across a page break, the marker is placed inline inside the joined
  paragraph, so the citation point is exact. A collapsed "Page list" navigation at the end links to
  every page.
- The EPUB 3 has a TOC, an `epub:type="page-list"` nav, `epub:type="pagebreak"` markers,
  EPUB Accessibility 1.1 metadata (`schema:accessMode`, `accessibilityFeature` including
  `printPageNumbers`, `accessibilityHazard`, `accessibilitySummary`) and validates cleanly with
  epubcheck 5.3.
- Running heads, footers and scanner stamps are dropped; the printed page number goes into the marker.

## Cost and model choice

Per page the model sees roughly 2,500 image tokens plus about 2,000 tokens of instructions and OCR
draft (the instruction block is prompt-cached), and writes about 1,000 tokens of JSON/HTML.
Rough cost per page at list prices:

| Model | Approx. per page | 50-page chapter |
|---|---|---|
| `claude-opus-5` ($5 / $25 per MTok) | $0.04 | $2 |
| `claude-sonnet-5` ($2 / $10 per MTok) | $0.02 | $1 |
| `claude-haiku-4-5` ($1 / $5 per MTok) | $0.01 | $0.50 |
| local Qwen2.5-VL on your GPU | electricity | free |

Opus is the default (best reading of tables, footnotes and degraded scans). For clean, plain text
pages Sonnet at `--effort low` is hard to tell apart. A sensible policy: run everything through the
local model or Sonnet, then re-run only `needs_review` pages and pages the coverage check flags
with `--force --model claude-opus-5`.

## Quality controls built in

- **Coverage check**: the transcribed word count of each page is compared with the OCR draft;
  pages under 60% or over 160% are flagged as possible omissions or invented text.
- **Model notes**: anything the model was unsure about marks the page `needs_review`.
- **HTML checks**: `lang`, `<title>`, exactly one h1, no skipped heading levels, alt on every image,
  header cells in tables, link text, unique ids, broken fragment links, page markers present.
- **epubcheck** (W3C) on the EPUB.

Automated checks cannot verify that the text is *correct*. For readings that go to a student with
an accommodation, a human should still spot-check the flagged pages against the scan; the
`get_page` tool shows the page image next to the stored HTML for exactly that purpose.

## Not done yet

- **Tagged PDF output.** HTML and EPUB were the priority. If PDF/UA is required later, the cleanest
  route is to generate a new PDF from the built HTML with a PDF/UA-capable HTML renderer; the scan's
  appearance is not preserved in that route.
- **Endnote linking across pages.** Footnotes on the same page are linked; references to endnotes at
  the end of a chapter are kept as plain superscripts.
- **Figure cropping quality** depends on the model's bounding boxes; check cropped figures.
- **Math** is transcribed as text or MathML by the model; nothing checks it.

## Development

```bash
pip install -e .[dev]
python -m pytest -q
```

Tests use a synthetic PDF and mocked HTTP, so they run without an API key or network.

## License

MIT, see [LICENSE](LICENSE).
