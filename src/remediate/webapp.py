"""Local web UI: a FastAPI app serving the review/edit page and a small JSON API over the same
document state the CLI and MCP server use. Run with `remediate ui`.
"""

from __future__ import annotations

import json
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

from .document import Document, parse_page_range, slugify
from .pdf import cached_page_png, new_document
from .pipeline import apply_result, ensure_draft_text
from .prompts import GUIDELINES
from .sanitize import sanitize_fragment

WEB_DIR = Path(__file__).parent / "web"

DEFAULT_SETTINGS: dict[str, Any] = {
    "backend": "anthropic",
    "model": "claude-opus-5",
    "effort": "medium",
    "anthropic_api_key": "",
    "openai_base_url": "http://localhost:11434/v1",
    "openai_api_key": "",
    "openai_model": "qwen2.5vl:7b",
    "workers": 4,
}


class PageUpdate(BaseModel):
    html: str = ""
    label: str | None = None
    starts_mid_paragraph: bool = False
    ends_mid_paragraph: bool = False
    skip: bool = False
    notes: str = ""
    status: str = "done"  # a human save marks the page reviewed unless told otherwise


class OpenRequest(BaseModel):
    pdf_path: str
    title: str = ""
    author: str = ""
    language: str = "en"


class TranscribeRequest(BaseModel):
    pages: str = "all"
    force: bool = False


def create_app(work_root: str | Path = "work", out_root: str | Path = "out") -> FastAPI:
    work_root = Path(work_root).resolve()
    out_root = Path(out_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    settings_path = work_root / "settings.json"
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.Lock()

    app = FastAPI(title="remediate", docs_url="/api/docs")

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
                "starts_mid_paragraph": p.starts_mid_paragraph, "ends_mid_paragraph": p.ends_mid_paragraph}

    def doc_view(doc: Document) -> dict[str, Any]:
        return {"doc_id": Path(doc.workdir).name, **doc.summary(), "pages": [page_view(p) for p in doc.pages],
                "job": active_job(Path(doc.workdir).name)}

    def active_job(doc_id: str) -> dict[str, Any] | None:
        with jobs_lock:
            for j in jobs.values():
                if j["doc_id"] == doc_id and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "thread"}
        return None

    def make_backend_from_settings():
        from .backends import make_backend

        s = load_settings()
        if s["backend"] == "anthropic":
            kw: dict[str, Any] = {"effort": s.get("effort") or "medium"}
            if s.get("anthropic_api_key"):
                kw["api_key"] = s["anthropic_api_key"]
            return make_backend("anthropic", s.get("model") or None, **kw)
        return make_backend("openai", s.get("openai_model") or None, base_url=s.get("openai_base_url") or None,
                            api_key=s.get("openai_api_key") or None)

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
            "model": p.model, "of": len(doc.pages),
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

    @app.put("/api/documents/{doc_id}/pages/{n}")
    def put_page(doc_id: str, n: int, upd: PageUpdate) -> dict[str, Any]:
        doc = load_doc(doc_id)
        try:
            p = doc.page(n)
        except IndexError as e:
            raise HTTPException(404, str(e)) from e
        clean = sanitize_fragment(upd.html)
        apply_result(p, {"label": upd.label, "skip": upd.skip, "starts_mid_paragraph": upd.starts_mid_paragraph,
                         "ends_mid_paragraph": upd.ends_mid_paragraph, "html": clean, "figures": [],
                         "notes": upd.notes}, model="editor")
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
        except Exception as e:  # noqa: BLE001 - surface config problems to the UI
            raise HTTPException(400, f"backend not configured: {e}") from e
        idx = parse_page_range(req.pages, len(doc.pages))
        job_id = uuid.uuid4().hex[:8]
        job: dict[str, Any] = {"id": job_id, "doc_id": doc_id, "status": "running", "requested": len(idx),
                               "completed": 0, "errors": 0, "last": "", "started": time.time(), "model": backend.model}

        def progress(page, status):
            job["completed"] += 1
            if status == "error" or page.status == "error":
                job["errors"] += 1
            job["last"] = f"page {page.index}: {status}"

        def run():
            try:
                job["summary"] = transcribe_pages(doc, backend, idx, force=req.force,
                                                  workers=int(load_settings().get("workers") or 4),
                                                  on_progress=progress)
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
