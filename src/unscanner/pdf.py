"""PDF access: page rendering, baseline text (text layer or OCR), page labels, metadata."""

from __future__ import annotations

import datetime as dt
import io
import re
import shutil
from functools import lru_cache
from pathlib import Path

import pymupdf

from .document import Document, Page, doc_id_for, find_project, fingerprint

TARGET_LONG_SIDE_PX = 1600  # good balance for vision models (Claude resizes above ~1568)
MIN_TEXT_CHARS = 40  # below this we assume there is no usable text layer

# Page renders and word boxes are made from the PDF on demand and kept to save the wait next time; they
# can be thrown away at any moment. By default they sit in <workdir>/pages; the installed app points
# cache_root at AppData\Local\Unscanner\cache (app.py), out of the synced Documents folder, and then
# each project's cache is <cache_root>/<doc_id>. Together they are kept under CACHE_MAX_BYTES: the
# projects used longest ago lose theirs first (trim_cache).
cache_root: Path | None = None
CACHE_DIR = "pages"
CACHE_MAX_BYTES = 2_000_000_000


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


def cache_dir(doc: Document) -> Path:
    """Where this project's page renders live (see cache_root). With a cache_root, a cache left in the
    work folder by an earlier version is dropped: it is only a copy of what the PDF holds."""
    wd = Path(doc.workdir)
    if cache_root is None:
        return wd / CACHE_DIR
    if (wd / CACHE_DIR).is_dir():
        shutil.rmtree(wd / CACHE_DIR, ignore_errors=True)
    return Path(cache_root) / wd.name


def cache_dirs(work_root: str | Path) -> list[Path]:
    """Every project cache folder there is (also those of projects removed meanwhile)."""
    if cache_root is None:
        return [d for d in Path(work_root).glob(f"*/{CACHE_DIR}") if d.is_dir()]
    return [d for d in Path(cache_root).glob("*/") if d.is_dir()] if Path(cache_root).is_dir() else []


def drop_cache(doc: Document) -> None:
    shutil.rmtree(cache_dir(doc), ignore_errors=True)


def _dir_size_and_age(d: Path) -> tuple[int, float]:
    files = [f for f in d.rglob("*") if f.is_file()]
    return sum(f.stat().st_size for f in files), max((f.stat().st_mtime for f in files), default=0.0)


def trim_cache(work_root: str | Path, keep: str = "", max_bytes: int = CACHE_MAX_BYTES) -> list[str]:
    """Drop whole project caches, least recently added to first, until the rest fit in max_bytes.
    `keep` is the doc_id of the project being worked on, which stays. Returns the doc_ids dropped."""
    sized = [(d, *_dir_size_and_age(d)) for d in cache_dirs(work_root)]
    total = sum(size for _, size, _ in sized)
    dropped = []
    for d, size, _age in sorted(sized, key=lambda t: t[2]):
        if total <= max_bytes:
            break
        doc_id = d.name if cache_root is not None else d.parent.name
        if doc_id == keep:
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= size
        dropped.append(doc_id)
    return dropped


def cached_page_png(doc: Document, index: int) -> Path:
    """Render once into the project's cache as pNNN.png and return the path."""
    out = cache_dir(doc) / f"p{index:03d}.png"
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

    out = cache_dir(doc) / f"p{index:03d}.words.json"
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


def crop_png(png: bytes, bbox: list[float], rotate: int = 0) -> bytes:
    """Crop a normalized [x0,y0,x1,y1] (0-1000) region out of a PNG, then turn it `rotate` degrees
    clockwise (a multiple of 90)."""
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    w, h = im.size
    x0, y0, x1, y1 = bbox
    box = (int(w * x0 / 1000), int(h * y0 / 1000), int(w * x1 / 1000), int(h * y1 / 1000))
    box = (max(0, box[0]), max(0, box[1]), min(w, max(box[2], box[0] + 1)), min(h, max(box[3], box[1] + 1)))
    buf = io.BytesIO()
    out = im.crop(box)
    if rotate % 360:
        out = out.rotate(-(rotate % 360), expand=True)  # PIL turns counterclockwise
    out.save(buf, format="PNG")
    return buf.getvalue()


JPEG_QUALITY = 80  # figures cropped from a scan: no visible loss in a halftone, about a fifth of the PNG


def compress_image(png: bytes, grayscale: bool = False, jpeg: bool = False) -> bytes:
    """A figure crop re-encoded for a smaller export: without colour, as a JPEG, or both. With neither
    the PNG comes back untouched."""
    if not (grayscale or jpeg):
        return png
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    if grayscale:
        im = im.convert("L")
    elif jpeg and im.mode not in ("L", "RGB"):
        im = im.convert("RGB")  # JPEG has no palette and no transparency
    buf = io.BytesIO()
    if jpeg:
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    else:
        im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class SourceMismatch(ValueError):
    """The PDF offered is not the one the project was made from."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Titles and authors that PDF software writes by itself: a converter's "Microsoft Word - <file name>",
# a file name, an id, a placeholder, a server's service account. The PDF's file name makes a better
# title than these, and no author is better than a machine's.
_APP_PREFIX = re.compile(r"^(microsoft\s+)?(word|excel|powerpoint|publisher|office)\s+-\s+", re.I)
_FILE_NAME = re.compile(r"\.(docx?|rtf|odt|wpd|pptx?|xlsx?|pdf|txt|html?|tiff?|jpe?g|png|indd|qxd|e?ps)$", re.I)
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-|[0-9a-f]{16}", re.I)
_PLACEHOLDER = re.compile(r"^(untitled|no title|title|none|unknown|(scanned |microsoft word )?"
                          r"(document|scan|image|page|presentation)) ?\d*$", re.I)
_MACHINE_AUTHOR = re.compile(r"^(svc|service)[-_.]|^(administrator|admin|user|owner|default|unknown|none|"
                             r"author|scanner|windows user|microsoft office user|registered user)$", re.I)


def usable_title(value: str | None) -> str:
    """The title stored in a PDF, or "" when software made it up rather than a person."""
    value = (value or "").strip()
    if (not value or _APP_PREFIX.match(value) or _FILE_NAME.search(value) or _ID.search(value)
            or _PLACEHOLDER.match(value) or ("_" in value and " " not in value)):
        return ""
    return value


def usable_author(value: str | None) -> str:
    """The author stored in a PDF, or "" when it is an account or placeholder rather than a person."""
    value = (value or "").strip()
    return "" if _MACHINE_AUTHOR.search(value) else value


def new_document(pdf_path: str | Path, work_root: str | Path, title: str = "", author: str = "",
                 language: str = "en") -> Document:
    """Open a PDF: its project when it has one here (found by the file's content, so a renamed or moved
    PDF finds its work again, and another PDF with the same name does not), else a new one. The PDF
    stays where it is; the project records its path."""
    pdf_path = Path(pdf_path).resolve()
    fp = fingerprint(pdf_path)
    workdir = find_project(work_root, fp, pdf_path.name)
    if workdir is not None:
        doc = Document.load(workdir)
        doc.source = str(pdf_path)  # the file the person just chose, wherever its earlier copy went
        doc.fingerprint = fp
        if title:
            doc.title = title
        if author:
            doc.author = author
        doc.opened_at = _now()
        doc.save()
        return doc
    workdir = Path(work_root).resolve() / doc_id_for(pdf_path, fp)
    with open_pdf(pdf_path) as pdf:
        meta = pdf.metadata or {}
        pages = []
        for i in range(len(pdf)):
            label = pdf[i].get_label() or None  # PDF page labels, when the file has them
            pages.append(Page(index=i + 1, label=label))
    doc = Document(
        source=str(pdf_path),
        workdir=str(workdir),
        title=title or usable_title(meta.get("title")) or pdf_path.stem,
        author=author or usable_author(meta.get("author")),
        language=language,
        pages=pages,
        fingerprint=fp,
        opened_at=_now(),
    )
    doc.save()
    return doc


def relink_source(doc: Document, pdf_path: str | Path) -> None:
    """Point a project whose PDF went missing at the file again (Locate in the UI). The file must be
    the same PDF: same fingerprint, or for a project made before fingerprints, the same page count."""
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise SourceMismatch(f"not a PDF file: {pdf_path}")
    fp = fingerprint(pdf_path)
    if doc.fingerprint:
        if fp != doc.fingerprint:
            raise SourceMismatch(f"{pdf_path.name} is not the PDF this project was made from")
    else:
        with open_pdf(pdf_path) as pdf:
            if len(pdf) != len(doc.pages):
                raise SourceMismatch(f"{pdf_path.name} has {len(pdf)} pages; this project has {len(doc.pages)}")
        doc.fingerprint = fp
    doc.source = str(pdf_path)
    doc.save()


def remove_project(doc: Document, work_root: str | Path) -> None:
    """Delete a project: its work folder, its cache, and its copy of the PDF when the PDF was uploaded
    into work/_inbox (a PDF anywhere else is the person's own file and stays)."""
    drop_cache(doc)
    src = Path(doc.source)
    inbox = Path(work_root).resolve() / "_inbox"
    if src.is_file() and src.parent == inbox:
        src.unlink(missing_ok=True)
    shutil.rmtree(doc.workdir, ignore_errors=True)
