"""Local web UI: a FastAPI app serving the review/edit page and a small JSON API over the same
document state the CLI and MCP server use. Run with `remediate ui`.
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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .backends.openai_compat import DEFAULT_MODEL as DEFAULT_OPENAI_MODEL
from .document import Document, parse_page_range, slugify
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


class DescribeRequest(BaseModel):
    caption: str = ""
    context: str = ""


class LocateRequest(BaseModel):
    context: list[str]
    index: int


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

    app = FastAPI(title="remediate", docs_url="/api/docs", lifespan=lifespan)
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
        return s

    def load_doc(doc_id: str) -> Document:
        wd = work_root / doc_id
        if not Document.exists(wd):
            raise HTTPException(404, f"unknown document {doc_id}")
        return Document.load(wd)

    def page_view(p) -> dict[str, Any]:
        return {"index": p.index, "label": p.label, "status": p.status, "skip": p.skip,
                "words": len((p.html or "").split()), "notes": p.notes,
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
            if s.get("anthropic_api_key"):
                kw["api_key"] = s["anthropic_api_key"]
            return make_backend("anthropic", s.get("model") or None, **kw)
        return make_backend("openai", s.get("openai_model") or None, base_url=s.get("openai_base_url") or None,
                            api_key=s.get("openai_api_key") or None,
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
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

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

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: str) -> dict[str, Any]:
        return doc_view(load_doc(doc_id))

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
        return FileResponse(cached_page_png(doc, n), media_type="image/png")

    @app.get("/api/documents/{doc_id}/pages/{n}/figure/{fid}")
    def get_page_figure(doc_id: str, n: int, fid: str, bbox: str | None = None):
        """Crop of a figure on this page, from its stored bbox or from ?bbox=x0,y0,x1,y1 (0-1000 page
        coordinates) for a live preview while the user adjusts the crop in the editor."""
        from fastapi.responses import Response

        from .pdf import crop_png

        doc = load_doc(doc_id)
        try:
            p = doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        box: list[float] | None = None
        if bbox:
            try:
                box = [float(v) for v in bbox.split(",")]
                assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
            except (ValueError, AssertionError) as e:
                raise HTTPException(400, "bbox must be x0,y0,x1,y1 with x0<x1 and y0<y1") from e
        else:
            fig = next((f for f in p.figures if f.id == fid), None)
            if fig is None or not fig.bbox:
                raise HTTPException(404, "no crop box for this figure")
            box = fig.bbox
        return Response(crop_png(cached_page_png(doc, n).read_bytes(), box), media_type="image/png")

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
            text = backend.describe_image(crop_png(cached_page_png(doc, n).read_bytes(), fig.bbox), prompt)
        except BackendError as e:
            raise HTTPException(502, str(e)) from e
        except Exception as e:  # noqa: BLE001 - surface the reason to the UI instead of a bare 500
            raise HTTPException(502, f"{type(e).__name__}: {e}") from e
        decorative = text.strip().upper().startswith("DECORATIVE")
        return {"alt": "" if decorative else text.strip(), "decorative": decorative, "model": backend.model}

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

    @app.get("/api/documents/{doc_id}/output/html")
    def output_html(doc_id: str):
        p = _out_dir(load_doc(doc_id)) / "index.html"
        if not p.exists():
            raise HTTPException(404, "no build yet")
        return FileResponse(p, media_type="text/html", filename=p.name)

    @app.get("/api/documents/{doc_id}/output/epub")
    def output_epub(doc_id: str):
        files = sorted(_out_dir(load_doc(doc_id)).glob("*.epub"))
        if not files:
            raise HTTPException(404, "no build yet")
        return FileResponse(files[-1], media_type="application/epub+zip", filename=files[-1].name)

    @app.get("/api/documents/{doc_id}/preview", response_class=HTMLResponse)
    def preview(doc_id: str) -> str:
        p = _out_dir(load_doc(doc_id)) / "index.html"
        if not p.exists():
            raise HTTPException(404, "no build yet")
        return p.read_text(encoding="utf-8")

    @app.get("/api/documents/{doc_id}/figures/{name}")
    def figure(doc_id: str, name: str):
        p = _out_dir(load_doc(doc_id)) / "figures" / Path(name).name
        if not p.exists():
            raise HTTPException(404, "no such figure")
        return FileResponse(p, media_type="image/png")

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
        return load_settings()

    @app.put("/api/settings")
    def put_settings(s: dict[str, Any]) -> dict[str, Any]:
        cur = load_settings()
        cur.update({k: v for k, v in s.items() if k in DEFAULT_SETTINGS})
        settings_path.write_text(json.dumps(cur, indent=1), encoding="utf-8")
        return cur

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
    print(f"remediate UI at {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
