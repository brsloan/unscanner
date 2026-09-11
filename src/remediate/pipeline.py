"""Batch transcription: run a model backend over pages and store results in the document state."""

from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .backends import Backend, BackendError
from .document import Document, Figure, Page
from .pdf import cached_page_png, draft_text_for_page
from .prompts import build_user_prompt

TAIL_CHARS = 400
HEAD_CHARS = 250


def ensure_draft_text(doc: Document, indexes: list[int], save: bool = True) -> None:
    """Populate page.draft_text (PDF text layer, else OCR) for the given pages and their neighbours."""
    wanted = set()
    for i in indexes:
        wanted.update({i - 1, i, i + 1})
    changed = False
    for i in sorted(wanted):
        if 1 <= i <= len(doc.pages):
            p = doc.page(i)
            if not p.draft_source:
                p.draft_text, p.draft_source = draft_text_for_page(doc, i)
                changed = True
    if changed and save:
        doc.save()


def page_prompt(doc: Document, index: int, instructions: str = "") -> str:
    page = doc.page(index)
    prev_tail = doc.page(index - 1).draft_text[-TAIL_CHARS:] if index > 1 else ""
    next_head = doc.page(index + 1).draft_text[:HEAD_CHARS] if index < len(doc.pages) else ""
    return build_user_prompt(index, len(doc.pages), page.draft_text, prev_tail, next_head,
                             doc.title, doc.language, instructions=instructions)


def apply_result(page: Page, result: dict, model: str = "", usage: dict | None = None,
                 changed_by: str | None = None) -> None:
    """Store a normalized transcription result on a page and stamp version/changed_by."""
    page.version += 1
    page.changed_by = changed_by or model or "unknown"
    page.updated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    page.label = result.get("label") or page.label
    page.skip = bool(result.get("skip"))
    page.starts_mid_paragraph = bool(result.get("starts_mid_paragraph"))
    page.ends_mid_paragraph = bool(result.get("ends_mid_paragraph"))
    page.html = result.get("html", "") or ""
    page.figures = [Figure(**f) for f in result.get("figures", [])]
    page.notes = result.get("notes", "") or ""
    page.model = model
    page.usage = usage or {}
    if not page.skip and not page.html.strip():
        page.status = "needs_review"
        page.notes = (page.notes + " | model returned empty html").strip(" |")
    else:
        page.status = "needs_review" if page.notes.strip() else "done"


def transcribe_pages(doc: Document, backend: Backend, indexes: list[int], force: bool = False,
                     workers: int = 4, on_progress: Callable[[Page, str], None] | None = None,
                     instructions: str = "") -> dict:
    """Transcribe the given pages with `backend`. Skips pages already done unless force=True.

    `instructions` is free text appended to every page prompt (e.g. "the equations were transcribed
    badly; write every display equation as MathML"). Returns a summary dict with counts and usage.
    """
    todo = [i for i in indexes if force or doc.page(i).status in ("pending", "error")]
    ensure_draft_text(doc, todo)
    lock = threading.Lock()
    usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    done = errors = 0

    def work(i: int) -> tuple[int, dict | None, dict | None, str | None]:
        png = cached_page_png(doc, i).read_bytes()
        prompt = page_prompt(doc, i, instructions)
        try:
            result, usage = backend.transcribe(png, prompt)
            return i, result, usage, None
        except BackendError as e:
            return i, None, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, i) for i in todo]
        for fut in as_completed(futures):
            i, result, usage, err = fut.result()
            page = doc.page(i)
            with lock:
                if err:
                    page.status = "error"
                    page.notes = err
                    errors += 1
                else:
                    apply_result(page, result, backend.model, usage)
                    for k in usage_total:
                        usage_total[k] += int((usage or {}).get(k, 0) or 0)
                    done += 1
                doc.save()
            if on_progress:
                on_progress(page, err or page.status)
    return {"requested": len(indexes), "processed": len(todo), "done": done, "errors": errors,
            "usage": usage_total, "model": backend.model}
