"""On-disk document state: one work directory per PDF, one JSON file describing every page."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_FILE = "doc.json"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:80] or "document"


@dataclass
class Figure:
    id: str
    alt: str
    bbox: list[float] | None = None  # [x0, y0, x1, y1] in 0-1000 normalized page coords
    caption: str = ""
    rotate: int = 0  # clockwise degrees applied after cropping: 0, 90, 180 or 270

    def __post_init__(self) -> None:
        # The HTML refers to a figure as src="fig:ID"; some models repeat that prefix in the id.
        self.id = self.id.removeprefix("fig:")
        self.rotate = normalize_rotation(self.rotate)


def normalize_rotation(value: object) -> int:
    """A rotation in degrees as one of 0, 90, 180, 270 (clockwise); anything unreadable is 0."""
    try:
        return round(float(value) / 90) % 4 * 90  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@dataclass
class Page:
    index: int  # 1-based position in the PDF
    label: str | None = None  # printed page number/label as it appears on the page
    html: str = ""  # body fragment for this page
    starts_mid_paragraph: bool = False
    ends_mid_paragraph: bool = False
    skip: bool = False  # blank page, or nothing but an unlabeled cover image
    status: str = "pending"  # pending | done | needs_review | error
    notes: str = ""
    draft_text: str = ""  # baseline text (PDF text layer or OCR), used for QA
    draft_source: str = ""  # "textlayer" | "ocr" | ""
    figures: list[Figure] = field(default_factory=list)
    model: str = ""
    usage: dict = field(default_factory=dict)
    version: int = 0  # bumped on every stored change; the UI sends it back to detect conflicts
    changed_by: str = ""  # "editor" (a person in the UI), "claude" (MCP set_page), or a model id
    updated_at: str = ""

    @staticmethod
    def from_dict(d: dict) -> "Page":
        figs = [Figure(**f) for f in d.get("figures", [])]
        d = {k: v for k, v in d.items() if k != "figures"}
        return Page(figures=figs, **d)


@dataclass
class Document:
    source: str  # absolute path to the PDF
    workdir: str  # absolute path to the work directory
    title: str = ""
    author: str = ""
    language: str = "en"
    pages: list[Page] = field(default_factory=list)

    # ---- persistence -------------------------------------------------
    @property
    def state_path(self) -> Path:
        return Path(self.workdir) / STATE_FILE

    def save(self) -> None:
        Path(self.workdir).mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def load(workdir: str | Path) -> "Document":
        p = Path(workdir) / STATE_FILE
        data = json.loads(p.read_text(encoding="utf-8"))
        pages = [Page.from_dict(pd) for pd in data.pop("pages", [])]
        return Document(pages=pages, **data)

    @staticmethod
    def exists(workdir: str | Path) -> bool:
        return (Path(workdir) / STATE_FILE).exists()

    # ---- helpers -----------------------------------------------------
    def page(self, index: int) -> Page:
        if not 1 <= index <= len(self.pages):
            raise IndexError(f"page {index} out of range 1..{len(self.pages)}")
        return self.pages[index - 1]

    def set_properties(self, title: str | None = None, author: str | None = None,
                       language: str | None = None) -> None:
        """Change the output metadata; None leaves a field as it is. Raises ValueError on bad input."""
        if title is not None:
            if not title.strip():
                raise ValueError("the title cannot be empty")
            self.title = title.strip()
        if author is not None:
            self.author = author.strip()
        if language is not None:
            if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*", language.strip()):
                raise ValueError(f"not a language code: {language!r} (use e.g. en, fr, en-GB)")
            self.language = language.strip()

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for p in self.pages:
            counts[p.status] = counts.get(p.status, 0) + 1
        return {
            "source": self.source,
            "workdir": self.workdir,
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "page_count": len(self.pages),
            "status_counts": counts,
            "pending_pages": [p.index for p in self.pages if p.status == "pending"],
            "needs_review": [p.index for p in self.pages if p.status == "needs_review"],
        }


def parse_page_range(spec: str | None, n_pages: int) -> list[int]:
    """'all' | '3' | '1-5' | '1-3,7,9-10' -> sorted list of 1-based page indexes."""
    if not spec or spec.strip().lower() == "all":
        return list(range(1, n_pages + 1))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a_i = int(a) if a.strip() else 1
            b_i = int(b) if b.strip() else n_pages
            out.update(range(max(1, a_i), min(n_pages, b_i) + 1))
        else:
            i = int(part)
            if 1 <= i <= n_pages:
                out.add(i)
    return sorted(out)
