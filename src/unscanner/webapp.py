"""Local web UI: a FastAPI app serving the review/edit page and a small JSON API over the same
document state the CLI and MCP server use. Run with `unscanner ui`.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import keystore, project
from .assemble import EXPORT_DEFAULTS, image_mime, outline
from .backends.openai_compat import DEFAULT_MODEL as DEFAULT_OPENAI_MODEL
from .document import Document, normalize_rotation, parse_page_range, slugify
from .pdf import cached_page_png, cached_page_words, new_document
from .pipeline import apply_result, ensure_draft_text
from .prompts import GUIDELINES
from .sanitize import sanitize_fragment
from .session import Session

WEB_DIR = Path(__file__).parent / "web"

DEFAULT_SETTINGS: dict[str, Any] = {
    "backend": "anthropic",
    "model": "claude-opus-5",
    "effort": "medium",
    "anthropic_api_key": "",
    "openai_base_url": "http://localhost:11434/v1",
    "openai_api_key": "",
    "openai_model": DEFAULT_OPENAI_MODEL,
    "openai_disable_thinking": True,
    "openai_max_tokens": 8000,  # output budget per page; a page that hits it is usually a model looping
    "workers": 4,
    # When the main backend refuses a page (safety/copyright), retry it once here: "anthropic",
    # "openai" or "none". Typically the local model when Claude is the main backend, or vice versa.
    "fallback_backend": "none",
    # Send the document title with every page. A recognisable title of a well-known work makes some
    # providers more likely to refuse; turn it off for a document that keeps getting refused.
    "send_title": True,
    # Export section: indent-style paragraphs, justification and page numbers, per output format.
    **EXPORT_DEFAULTS,
}


class PageUpdate(BaseModel):
    html: str = ""
    label: str | None = None
    starts_mid_paragraph: bool = False
    ends_mid_paragraph: bool = False
    skip: bool = False
    notes: str = ""
    status: str = "done"  # a human save marks the page reviewed unless told otherwise
    version: int | None = None  # version the editor loaded; a stale value is rejected with 409
    figures: list[dict] | None = None  # None keeps the page's stored figures (crop boxes) unchanged


class BulkPageUpdate(BaseModel):
    pages: list[int]
    action: str  # "skip" | "unskip" | "approve" | "needs_review"


class ViewUpdate(BaseModel):
    doc_id: str
    page: int
    label: str | None = None
    selection: str = ""
    dirty: bool = False


class OpenRequest(BaseModel):
    pdf_path: str
    title: str = ""
    author: str = ""
    language: str = "en"


class PropertiesUpdate(BaseModel):
    title: str
    author: str = ""
    language: str = "en"


class DescribeRequest(BaseModel):
    caption: str = ""
    context: str = ""


class TableRequest(BaseModel):
    bbox: list[float]
    text: str = ""


TABLE_MAX_TOKENS = 4000  # output budget for one region re-read as a table (+Table in the UI)


class LocateRequest(BaseModel):
    context: list[str]
    index: int


class DiffRequest(BaseModel):
    words: list[str]


class TranscribeRequest(BaseModel):
    pages: str = "all"
    force: bool = False
    instructions: str = ""


def create_app(work_root: str | Path = "work", out_root: str | Path = "out", mount_mcp: bool = True) -> FastAPI:
    work_root = Path(work_root).resolve()
    out_root = Path(out_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    settings_path = work_root / "settings.json"
    session = Session(work_root)
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.Lock()

    # The MCP server shares this process (and the session file) when mounted at /mcp, so Claude
    # Desktop / Claude Code can connect to http://127.0.0.1:<port>/mcp while the UI is open.
    from . import mcp_server

    mcp_server.configure(work_root, out_root)
    mcp_app = mcp_server.server.streamable_http_app(streamable_http_path="/", stateless_http=True) if mount_mcp else None

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_app is not None:
            async with mcp_server.server.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="unscanner", docs_url="/api/docs", lifespan=lifespan)
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    # ---------------------------------------------------------------- helpers
    def load_settings() -> dict[str, Any]:
        s = dict(DEFAULT_SETTINGS)
        if settings_path.exists():
            try:
                s.update(json.loads(settings_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        # A key left in the file in plain text moves to the OS credential store when there is one.
        moved = [k for k in keystore.SECRET_KEYS if s.get(k) and keystore.set_secret(k, str(s[k]))]
        if moved:
            s.update(dict.fromkeys(moved, ""))
            save_settings(s)
        return s

    def save_settings(s: dict[str, Any]) -> None:
        settings_path.write_text(json.dumps(s, indent=1), encoding="utf-8")

    def secret(s: dict[str, Any], name: str) -> str:
        """An API key: from settings.json (no credential store on this machine), else the store."""
        return s.get(name) or keystore.get_secret(name)

    def public_settings(s: dict[str, Any]) -> dict[str, Any]:
        """Settings as the browser sees them: API keys never leave the server, only whether one is saved."""
        out = dict(s)
        for k in keystore.SECRET_KEYS:
            out[k + "_set"] = bool(secret(s, k))
            out[k] = ""
        out["key_storage"] = "keyring" if keystore.available() else "file"
        return out

    def load_doc(doc_id: str) -> Document:
        wd = work_root / doc_id
        if not Document.exists(wd):
            raise HTTPException(404, f"unknown document {doc_id}")
        return Document.load(wd)

    def page_view(p) -> dict[str, Any]:
        return {"index": p.index, "label": p.label, "status": p.status, "skip": p.skip,
                "words": len((p.html or "").split()), "notes": p.notes,
                # for the page list's "Has figures" / "Has tables" filters
                "has_figures": "<img" in (p.html or ""), "has_tables": "<table" in (p.html or ""),
                "starts_mid_paragraph": p.starts_mid_paragraph, "ends_mid_paragraph": p.ends_mid_paragraph,
                "version": p.version, "changed_by": p.changed_by, "updated_at": p.updated_at}

    def doc_view(doc: Document) -> dict[str, Any]:
        return {"doc_id": Path(doc.workdir).name, **doc.summary(), "pages": [page_view(p) for p in doc.pages],
                "job": active_job(Path(doc.workdir).name)}

    def active_job(doc_id: str) -> dict[str, Any] | None:
        with jobs_lock:
            for j in jobs.values():
                if j["doc_id"] == doc_id and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "thread"}
        return None

    def backend_from_settings(s: dict[str, Any], name: str):
        from .backends import make_backend

        if name == "anthropic":
            kw: dict[str, Any] = {"effort": s.get("effort") or "medium"}
            if secret(s, "anthropic_api_key"):
                kw["api_key"] = secret(s, "anthropic_api_key")
            return make_backend("anthropic", s.get("model") or None, **kw)
        return make_backend("openai", s.get("openai_model") or None, base_url=s.get("openai_base_url") or None,
                            api_key=secret(s, "openai_api_key") or None,
                            disable_thinking=bool(s.get("openai_disable_thinking", True)),
                            max_tokens=int(s.get("openai_max_tokens") or DEFAULT_SETTINGS["openai_max_tokens"]))

    def make_backend_from_settings():
        s = load_settings()
        return backend_from_settings(s, s["backend"])

    def make_fallback_from_settings():
        """The backend refused pages are retried on, or None when not configured (or same as main)."""
        s = load_settings()
        name = (s.get("fallback_backend") or "none").lower()
        if name in ("", "none") or name == s["backend"]:
            return None
        return backend_from_settings(s, name)

    # ---------------------------------------------------------------- pages
    # The UI's own files are always revalidated: otherwise a browser keeps running an old app.js for
    # hours after the code changes (StaticFiles sends no Cache-Control, so browsers guess).
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-cache"})

    # Pages without the <link rel="icon"> (a built document, the API) make browsers ask for this.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ---------------------------------------------------------------- documents
    @app.get("/api/documents")
    def list_documents() -> list[dict[str, Any]]:
        out = []
        for wd in sorted(work_root.glob("*/")):
            if Document.exists(wd):
                d = Document.load(wd)
                out.append({"doc_id": wd.name, **d.summary()})
        return out

    @app.post("/api/documents")
    def open_document(req: OpenRequest) -> dict[str, Any]:
        p = Path(req.pdf_path)
        if not p.exists() or p.suffix.lower() != ".pdf":
            raise HTTPException(400, f"not a PDF file: {req.pdf_path}")
        doc = new_document(p, work_root, title=req.title, author=req.author, language=req.language)
        return doc_view(doc)

    @app.post("/api/documents/upload")
    def upload_document(file: UploadFile = File(...), title: str = Form(""), author: str = Form(""),
                        language: str = Form("en")) -> dict[str, Any]:
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "upload a .pdf file")
        inbox = work_root / "_inbox"
        inbox.mkdir(exist_ok=True)
        dest = inbox / Path(file.filename).name
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        doc = new_document(dest, work_root, title=title, author=author, language=language)
        return doc_view(doc)

    @app.get("/api/documents/{doc_id}/export")
    def export_document(doc_id: str) -> JSONResponse:
        """The whole project as one JSON file to keep next to the PDF (backup, or another machine)."""
        doc = load_doc(doc_id)
        name = project.project_filename(doc)
        disposition = f"attachment; filename=\"{slugify(Path(doc.source).stem)}{project.SUFFIX}\"; filename*=UTF-8''{quote(name)}"
        return JSONResponse(project.export_project(doc), headers={"Content-Disposition": disposition})

    @app.post("/api/projects/import")
    def import_document(project_file: UploadFile = File(...), pdf: UploadFile | None = File(None),
                        pdf_path: str = Form(""), replace: bool = Form(False)) -> dict[str, Any]:
        """Restore an exported project. The PDF comes as an upload or a path; with neither, a copy
        this work directory already has (same file name) is used."""
        try:
            data = json.loads(project_file.file.read().decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(400, "not an Unscanner project file") from e
        inbox = work_root / "_inbox"
        if pdf is not None and pdf.filename:
            if not pdf.filename.lower().endswith(".pdf"):
                raise HTTPException(400, "upload a .pdf file")
            src = inbox / Path(pdf.filename).name
        elif pdf_path.strip():
            src = Path(pdf_path.strip())
            if not src.exists() or src.suffix.lower() != ".pdf":
                raise HTTPException(400, f"not a PDF file: {pdf_path}")
        else:
            name = Path(str(data.get("pdf_name") or "") if isinstance(data, dict) else "").name
            wd = work_root / slugify(Path(name).stem)
            known = [Path(Document.load(wd).source)] if name and Document.exists(wd) else []
            src = next((p for p in [*known, inbox / name] if name and p.is_file()), None)
            if src is None:
                raise HTTPException(400, f"choose the PDF this project was made from{f' ({name})' if name else ''}")
        doc_id = slugify(src.stem)
        if Document.exists(work_root / doc_id):
            if not replace:
                raise HTTPException(409, f"{src.name} already has a project here")
            if active_job(doc_id):
                raise HTTPException(409, "a transcription job is running for this document; try again when it finishes")
        if pdf is not None and pdf.filename:
            inbox.mkdir(exist_ok=True)
            with src.open("wb") as fh:
                shutil.copyfileobj(pdf.file, fh)
        try:
            doc = project.import_project(data, src, work_root, replace=replace)
        except project.ProjectExistsError as e:
            raise HTTPException(409, str(e)) from e
        except project.ProjectError as e:
            raise HTTPException(400, str(e)) from e
        return doc_view(doc)

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: str) -> dict[str, Any]:
        return doc_view(load_doc(doc_id))

    @app.get("/api/documents/{doc_id}/outline")
    def get_outline(doc_id: str) -> dict[str, Any]:
        """Every heading in the saved page HTML, for the Headings tab of the sidebar."""
        return {"headings": outline(load_doc(doc_id))}

    @app.put("/api/documents/{doc_id}/properties")
    def put_properties(doc_id: str, upd: PropertiesUpdate) -> dict[str, Any]:
        """Title, author and language used for the HTML <title>/lang and the EPUB metadata."""
        doc = load_doc(doc_id)
        if active_job(doc_id):
            # the job saves its own copy of the document after every page and would undo this change
            raise HTTPException(409, "a transcription job is running for this document; try again when it finishes")
        try:
            doc.set_properties(upd.title, upd.author, upd.language)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        doc.save()
        return doc_view(doc)

    @app.get("/api/documents/{doc_id}/pages/{n}")
    def get_page(doc_id: str, n: int) -> dict[str, Any]:
        doc = load_doc(doc_id)
        try:
            p = doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        ensure_draft_text(doc, [n])
        return {
            **page_view(p), "html": p.html, "draft_text": p.draft_text, "draft_source": p.draft_source,
            "model": p.model, "of": len(doc.pages), "figures": [asdict(f) for f in p.figures],
            "previous_page_ends_with": doc.page(n - 1).draft_text[-300:] if n > 1 else "",
            "next_page_starts_with": doc.page(n + 1).draft_text[:200] if n < len(doc.pages) else "",
        }

    @app.get("/api/documents/{doc_id}/pages/{n}/image")
    def get_page_image(doc_id: str, n: int):
        doc = load_doc(doc_id)
        try:
            doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        # Revalidated (cheap: ETag) so a page whose render changes, e.g. pages inserted into the
        # PDF, never shows a stale scan from the browser cache.
        return FileResponse(cached_page_png(doc, n), media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/documents/{doc_id}/pages/{n}/figure/{fid}")
    def get_page_figure(doc_id: str, n: int, fid: str, bbox: str | None = None, rotate: int | None = None):
        """Crop of a figure on this page, from its stored bbox and rotation or from ?bbox=x0,y0,x1,y1
        (0-1000 page coordinates) and ?rotate=90 for a live preview while the user adjusts the figure in
        the editor."""
        from fastapi.responses import Response

        from .pdf import crop_png

        doc = load_doc(doc_id)
        try:
            p = doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        fig = next((f for f in p.figures if f.id == fid), None)
        box: list[float] | None = None
        if bbox:
            try:
                box = [float(v) for v in bbox.split(",")]
                assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
            except (ValueError, AssertionError) as e:
                raise HTTPException(400, "bbox must be x0,y0,x1,y1 with x0<x1 and y0<y1") from e
        else:
            if fig is None or not fig.bbox:
                raise HTTPException(404, "no crop box for this figure")
            box = fig.bbox
        turn = normalize_rotation(rotate) if rotate is not None else (fig.rotate if fig else 0)
        return Response(crop_png(cached_page_png(doc, n).read_bytes(), box, turn), media_type="image/png",
                        headers={"Cache-Control": "no-cache"})

    @app.post("/api/documents/{doc_id}/pages/{n}/figures/{fid}/describe")
    def describe_figure(doc_id: str, n: int, fid: str, req: DescribeRequest) -> dict[str, Any]:
        """Ask the configured model for alt text for a figure crop. Returns {alt, decorative}."""
        from .backends import BackendError
        from .backends.base import ALT_TEXT_PROMPT
        from .pdf import crop_png

        doc = load_doc(doc_id)
        try:
            p = doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        fig = next((f for f in p.figures if f.id == fid), None)
        if fig is None or not fig.bbox:
            raise HTTPException(404, "no crop box for this figure")
        try:
            backend = make_backend_from_settings()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"backend not configured: {e}") from e
        prompt = ALT_TEXT_PROMPT
        if req.caption.strip():
            prompt += f"\nCaption printed with the image: {req.caption.strip()}"
        if req.context.strip():
            prompt += f"\nText near the image on the page: {req.context.strip()[:1500]}"
        try:
            text = backend.describe_image(crop_png(cached_page_png(doc, n).read_bytes(), fig.bbox, fig.rotate), prompt)
        except BackendError as e:
            raise HTTPException(502, str(e)) from e
        except Exception as e:  # noqa: BLE001 - surface the reason to the UI instead of a bare 500
            raise HTTPException(502, f"{type(e).__name__}: {e}") from e
        decorative = text.strip().upper().startswith("DECORATIVE")
        return {"alt": "" if decorative else text.strip(), "decorative": decorative, "model": backend.model}

    @app.post("/api/documents/{doc_id}/pages/{n}/table")
    def read_table(doc_id: str, n: int, req: TableRequest) -> dict[str, Any]:
        """Ask the configured model to re-read a region of the scan (req.bbox, 0-1000 page coordinates)
        as a table. Returns {html, model}; nothing is stored, the editor inserts the table and the
        person saves the page as usual. req.text is the editor's current wording of the region; without
        it the scan's words inside the box are sent as the hint."""
        from .backends import BackendError
        from .pdf import crop_png
        from .prompts import build_table_prompt, extract_table_html

        doc = load_doc(doc_id)
        try:
            doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        box = req.bbox
        if len(box) != 4 or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
            raise HTTPException(400, "bbox must be [x0, y0, x1, y1] in 0-1000 page coordinates with x0<x1 and y0<y1")
        text = req.text.strip()
        if not text:
            inside = [w["text"] for w in cached_page_words(doc, n)
                      if box[0] <= (w["x0"] + w["x1"]) / 2 <= box[2] and box[1] <= (w["y0"] + w["y1"]) / 2 <= box[3]]
            text = " ".join(inside)
        try:
            backend = make_backend_from_settings()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"backend not configured: {e}") from e
        try:
            reply = backend.describe_image(crop_png(cached_page_png(doc, n).read_bytes(), box),
                                           build_table_prompt(text), max_tokens=TABLE_MAX_TOKENS)
        except BackendError as e:
            raise HTTPException(502, str(e)) from e
        except Exception as e:  # noqa: BLE001 - surface the reason to the UI instead of a bare 500
            raise HTTPException(502, f"{type(e).__name__}: {e}") from e
        try:
            html = sanitize_fragment(extract_table_html(reply))
        except ValueError as e:
            raise HTTPException(502, f"{e}: {reply.strip()[:200]}") from e
        return {"html": html, "model": backend.model}

    @app.get("/api/documents/{doc_id}/pages/{n}/words")
    def get_page_words(doc_id: str, n: int) -> list[dict[str, Any]]:
        """Word boxes on the scan (0-1000 page coordinates, reading order), for the follow/marker feature."""
        doc = load_doc(doc_id)
        try:
            doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        return cached_page_words(doc, n)

    @app.post("/api/documents/{doc_id}/pages/{n}/locate")
    def locate_on_page(doc_id: str, n: int, req: LocateRequest) -> dict[str, Any]:
        """Find the page word matching the editor caret: req.context are the words around the caret,
        req.index is which of them the caret is on. Returns {found: bool, box?: {...}}."""
        from .locate import locate

        doc = load_doc(doc_id)
        try:
            doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        box = locate(cached_page_words(doc, n), req.context, req.index)
        return {"found": box is not None, "box": box}

    @app.post("/api/documents/{doc_id}/pages/{n}/diff")
    def diff_page(doc_id: str, n: int, req: DiffRequest) -> dict[str, Any]:
        """Word-level diff of the scan's words against req.words, the words now in the editor (saved
        or not). See diff.word_diff for the result."""
        from .diff import word_diff

        doc = load_doc(doc_id)
        try:
            doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        return word_diff(cached_page_words(doc, n), req.words)

    @app.put("/api/documents/{doc_id}/pages/{n}")
    def put_page(doc_id: str, n: int, upd: PageUpdate) -> dict[str, Any]:
        doc = load_doc(doc_id)
        try:
            p = doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        if upd.version is not None and upd.version != p.version:
            raise HTTPException(409, f"page {n} was changed by {p.changed_by or 'someone else'} while you were "
                                     f"editing (version {p.version}, you loaded {upd.version}); reload it first")
        clean = sanitize_fragment(upd.html)
        figures = upd.figures if upd.figures is not None else [asdict(f) for f in p.figures]
        apply_result(p, {"label": upd.label, "skip": upd.skip, "starts_mid_paragraph": upd.starts_mid_paragraph,
                         "ends_mid_paragraph": upd.ends_mid_paragraph, "html": clean, "figures": figures,
                         "notes": upd.notes}, model="editor", changed_by="editor")
        if upd.status in ("done", "needs_review", "pending"):
            p.status = upd.status if (clean or upd.skip) else "pending"
        doc.save()
        return {**page_view(p), "html": p.html}

    @app.post("/api/documents/{doc_id}/pages/bulk")
    def bulk_pages(doc_id: str, upd: BulkPageUpdate) -> dict[str, Any]:
        """Skip, un-skip, approve or flag several pages at once (the page list's right-click menu).

        Only the skip flag and the status change; each page keeps its content. Every page still goes
        through apply_result, so its version is bumped and an editor holding the old one gets a 409.
        A page with nothing on it cannot be approved: it stays "pending" unless it is skipped.
        Skip and un-skip leave the status alone: pages may be skipped just for one build (an export of
        part of the document), and that must not change the record of what has been approved.
        """
        if upd.action not in ("skip", "unskip", "approve", "needs_review"):
            raise HTTPException(400, f"unknown action {upd.action!r}")
        doc = load_doc(doc_id)
        try:
            pages = [doc.page(n) for n in dict.fromkeys(upd.pages)]
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        for p in pages:
            status = {"approve": "done", "needs_review": "needs_review"}.get(upd.action, p.status)
            skip = {"skip": True, "unskip": False}.get(upd.action, p.skip)
            apply_result(p, {"label": p.label, "skip": skip, "starts_mid_paragraph": p.starts_mid_paragraph,
                             "ends_mid_paragraph": p.ends_mid_paragraph, "html": p.html,
                             "figures": [asdict(f) for f in p.figures], "notes": p.notes},
                         model="editor", changed_by="editor")
            p.status = status if (p.html.strip() or p.skip) else "pending"
        doc.save()
        return {"pages": [page_view(p) for p in pages]}

    # ---------------------------------------------------------------- transcription jobs
    @app.post("/api/documents/{doc_id}/transcribe")
    def transcribe(doc_id: str, req: TranscribeRequest) -> dict[str, Any]:
        from .pipeline import transcribe_pages

        doc = load_doc(doc_id)
        if active_job(doc_id):
            raise HTTPException(409, "a transcription job is already running for this document")
        try:
            backend = make_backend_from_settings()
            fallback = make_fallback_from_settings()
        except Exception as e:  # noqa: BLE001 - surface config problems to the UI
            raise HTTPException(400, f"backend not configured: {e}") from e
        idx = parse_page_range(req.pages, len(doc.pages))
        job_id = uuid.uuid4().hex[:8]
        job: dict[str, Any] = {"id": job_id, "doc_id": doc_id, "status": "running", "requested": len(idx),
                               "completed": 0, "errors": 0, "last": "", "started": time.time(), "model": backend.model,
                               "fallback_model": fallback.model if fallback else None}

        def progress(page, status):
            job["completed"] += 1
            if status == "error" or page.status == "error":
                job["errors"] += 1
            job["last"] = f"page {page.index}: {status}"

        def run():
            try:
                s = load_settings()
                job["summary"] = transcribe_pages(doc, backend, idx, force=req.force,
                                                  workers=int(s.get("workers") or 4),
                                                  on_progress=progress, instructions=req.instructions,
                                                  fallback=fallback, send_title=bool(s.get("send_title", True)))
                job["status"] = "finished"
            except Exception as e:  # noqa: BLE001
                job["status"] = "error"
                job["error"] = str(e)

        with jobs_lock:
            jobs[job_id] = job
        threading.Thread(target=run, daemon=True).start()
        return {k: v for k, v in job.items()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        return {k: v for k, v in job.items()}

    # ---------------------------------------------------------------- build / validate / output
    @app.post("/api/documents/{doc_id}/build")
    def build(doc_id: str) -> dict[str, Any]:
        from .cli import _build

        return _build(load_doc(doc_id), str(out_root))

    @app.get("/api/documents/{doc_id}/validate")
    def validate(doc_id: str, epubcheck: bool = True) -> dict[str, Any]:
        from .cli import _validate

        return _validate(load_doc(doc_id), str(out_root), run_epubcheck=epubcheck)

    def _out_dir(doc: Document) -> Path:
        return out_root / slugify(Path(doc.source).stem)

    def _built_html(doc_id: str) -> Path:
        from .cli import built_html

        p = built_html(_out_dir(load_doc(doc_id)))
        if not p:
            raise HTTPException(404, "no build yet")
        return p

    @app.get("/api/documents/{doc_id}/output/html")
    def output_html(doc_id: str):
        p = _built_html(doc_id)
        return FileResponse(p, media_type="text/html", filename=p.name)

    @app.get("/api/documents/{doc_id}/output/epub")
    def output_epub(doc_id: str):
        files = sorted(_out_dir(load_doc(doc_id)).glob("*.epub"))
        if not files:
            raise HTTPException(404, "no build yet")
        return FileResponse(files[-1], media_type="application/epub+zip", filename=files[-1].name)

    @app.get("/api/documents/{doc_id}/preview", response_class=HTMLResponse)
    def preview(doc_id: str) -> str:
        p = _built_html(doc_id)
        return p.read_text(encoding="utf-8")

    @app.get("/api/documents/{doc_id}/figures/{name}")
    def figure(doc_id: str, name: str):
        p = _out_dir(load_doc(doc_id)) / "figures" / Path(name).name
        if not p.exists():
            raise HTTPException(404, "no such figure")
        return FileResponse(p, media_type=image_mime(p.name))

    # ---------------------------------------------------------------- collaboration session
    @app.get("/api/session")
    def get_session() -> dict[str, Any]:
        """What the UI shows (view) and any navigation an agent requested (requested)."""
        return session.get()

    @app.put("/api/session/view")
    def put_view(v: ViewUpdate) -> dict[str, Any]:
        return session.update_view(v.doc_id, v.page, v.label, v.selection, v.dirty)

    @app.delete("/api/session/requested")
    def clear_requested() -> dict[str, Any]:
        session.clear_request()
        return session.get()

    # ---------------------------------------------------------------- settings / misc
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return public_settings(load_settings())

    @app.put("/api/settings")
    def put_settings(s: dict[str, Any]) -> dict[str, Any]:
        cur = load_settings()
        for k in keystore.SECRET_KEYS:
            if k not in s:
                continue
            v = s.pop(k)
            if v == "":
                continue  # the browser never gets the saved key, so an empty field means "keep it"
            v = "" if v is None else str(v)  # null forgets the saved key
            cur[k] = "" if keystore.set_secret(k, v) else v  # no credential store: keep it in the file
        cur.update({k: v for k, v in s.items() if k in DEFAULT_SETTINGS})
        save_settings(cur)
        return public_settings(cur)

    @app.get("/api/guidelines")
    def guidelines() -> JSONResponse:
        return JSONResponse({"guidelines": GUIDELINES})

    return app


def serve(work_root: str = "work", out_root: str = "out", host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> None:
    import webbrowser

    import uvicorn

    app = create_app(work_root, out_root)
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"unscanner UI at {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
