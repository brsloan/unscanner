"""PDF access: page rendering, baseline text (text layer or OCR), page labels, metadata."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import pymupdf

from .document import Document, Page, slugify

TARGET_LONG_SIDE_PX = 1600  # good balance for vision models (Claude resizes above ~1568)
MIN_TEXT_CHARS = 40  # below this we assume there is no usable text layer


def open_pdf(path: str | Path) -> pymupdf.Document:
    return pymupdf.open(str(path))


def render_page_png(pdf_path: str | Path, index: int, long_side: int = TARGET_LONG_SIDE_PX) -> bytes:
    """Render 1-based page `index` to PNG bytes with the long side ~= long_side pixels."""
    with open_pdf(pdf_path) as doc:
        page = doc[index - 1]
        w, h = page.rect.width, page.rect.height
        scale = long_side / max(w, h)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")


def cached_page_png(doc: Document, index: int) -> Path:
    """Render once into <workdir>/pages/pNNN.png and return the path."""
    out = Path(doc.workdir) / "pages" / f"p{index:03d}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(render_page_png(doc.source, index))
    return out


def text_layer(pdf_path: str | Path, index: int) -> str:
    with open_pdf(pdf_path) as doc:
        return doc[index - 1].get_text("text").strip()


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr_onnxruntime import RapidOCR  # lazy: heavy import

    return RapidOCR()


def ocr_png(png: bytes) -> str:
    """Fallback OCR with RapidOCR (ONNX, CPU). Returns text in reading order (top-to-bottom)."""
    import numpy as np
    from PIL import Image

    img = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    result, _ = _ocr_engine()(img)
    if not result:
        return ""
    # result rows: [box(4 points), text, score]; sort by top y (bucketed) then x
    rows = sorted(result, key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
    return "\n".join(r[1] for r in rows)


def draft_text_for_page(doc: Document, index: int) -> tuple[str, str]:
    """Return (text, source) where source is 'textlayer' or 'ocr'."""
    txt = text_layer(doc.source, index)
    if len(txt) >= MIN_TEXT_CHARS:
        return txt, "textlayer"
    png = cached_page_png(doc, index).read_bytes()
    return ocr_png(png), "ocr"


def crop_png(png: bytes, bbox: list[float]) -> bytes:
    """Crop a normalized [x0,y0,x1,y1] (0-1000) region out of a PNG."""
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    w, h = im.size
    x0, y0, x1, y1 = bbox
    box = (int(w * x0 / 1000), int(h * y0 / 1000), int(w * x1 / 1000), int(h * y1 / 1000))
    box = (max(0, box[0]), max(0, box[1]), min(w, max(box[2], box[0] + 1)), min(h, max(box[3], box[1] + 1)))
    buf = io.BytesIO()
    im.crop(box).save(buf, format="PNG")
    return buf.getvalue()


def new_document(pdf_path: str | Path, work_root: str | Path, title: str = "", author: str = "",
                 language: str = "en") -> Document:
    """Create (or reopen) the work directory for a PDF and populate page records."""
    pdf_path = Path(pdf_path).resolve()
    workdir = Path(work_root).resolve() / slugify(pdf_path.stem)
    if Document.exists(workdir):
        doc = Document.load(workdir)
        if title:
            doc.title = title
        if author:
            doc.author = author
        doc.save()
        return doc
    with open_pdf(pdf_path) as pdf:
        meta = pdf.metadata or {}
        pages = []
        for i in range(len(pdf)):
            label = pdf[i].get_label() or None  # PDF page labels, when the file has them
            pages.append(Page(index=i + 1, label=label))
    doc = Document(
        source=str(pdf_path),
        workdir=str(workdir),
        title=title or (meta.get("title") or "").strip() or pdf_path.stem,
        author=author or (meta.get("author") or "").strip(),
        language=language,
        pages=pages,
    )
    doc.save()
    return doc
