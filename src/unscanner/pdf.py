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


def text_layer_words(pdf_path: str | Path, index: int) -> list[dict]:
    """Words with boxes from the PDF text layer, normalized to 0-1000 page coordinates, reading order."""
    with open_pdf(pdf_path) as doc:
        page = doc[index - 1]
        w, h = page.rect.width, page.rect.height
        raw = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, wordno)
    raw.sort(key=lambda t: (t[5], t[6], t[7]))
    clamp = lambda v: max(0.0, min(1000.0, v))  # noqa: E731 - text can sit outside the media box in odd PDFs
    return [{"text": t[4], "x0": clamp(t[0] / w * 1000), "y0": clamp(t[1] / h * 1000),
             "x1": clamp(t[2] / w * 1000), "y1": clamp(t[3] / h * 1000)}
            for t in raw if t[4].strip()]


def ocr_words(png: bytes) -> list[dict]:
    """Word boxes from RapidOCR line boxes: each line's text is split on spaces and the box width is
    divided among the words in proportion to their length."""
    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = im.size
    result, _ = _ocr_engine()(np.array(im))
    words: list[dict] = []
    if not result:
        return words
    rows = sorted(result, key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
    for box, text, _score in rows:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        toks = text.split()
        total = sum(len(t) for t in toks) + max(0, len(toks) - 1)
        cursor = x0
        for t in toks:
            frac = (len(t) + 1) / total if total else 1
            wx1 = cursor + (x1 - x0) * frac
            words.append({"text": t, "x0": cursor / W * 1000, "y0": y0 / H * 1000, "x1": wx1 / W * 1000, "y1": y1 / H * 1000})
            cursor = wx1
    return words


def cached_page_words(doc: Document, index: int) -> list[dict]:
    """Word boxes for a page (text layer, else OCR), cached as JSON next to the page render."""
    import json

    out = Path(doc.workdir) / "pages" / f"p{index:03d}.words.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    words = text_layer_words(doc.source, index)
    if len(" ".join(w["text"] for w in words)) < MIN_TEXT_CHARS:
        words = ocr_words(cached_page_png(doc, index).read_bytes())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(words), encoding="utf-8")
    return words


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
