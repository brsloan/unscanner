"""Automated accessibility and completeness checks.

- validate_html: structural WCAG-oriented checks on the assembled HTML (language, title, headings,
  alt text, tables, links, page markers, ids).
- coverage: per-page comparison of transcribed word count vs. the draft OCR text, to catch pages the
  model skipped or padded.
- epubcheck: runs the W3C epubcheck jar when Java and the jar are available.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from lxml import html as lhtml

from .document import Document

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass
class Issue:
    severity: str  # error | warning | info
    code: str
    message: str
    location: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def validate_html(html_text: str) -> list[Issue]:
    issues: list[Issue] = []
    root = lhtml.fromstring(html_text)
    html_el = root if root.tag == "html" else root.getroottree().getroot()
    if not (html_el.get("lang") or "").strip():
        issues.append(Issue("error", "lang", "<html> is missing a lang attribute (WCAG 3.1.1)"))
    title = root.find(".//title")
    if title is None or not (title.text or "").strip():
        issues.append(Issue("error", "title", "document has no <title> (WCAG 2.4.2)"))

    heads = [el for el in root.iter() if isinstance(el.tag, str) and el.tag in HEADINGS]
    h1s = [h for h in heads if h.tag == "h1"]
    if len(h1s) != 1:
        issues.append(Issue("warning", "h1-count", f"expected exactly one <h1>, found {len(h1s)}"))
    prev = 0
    for h in heads:
        lvl = int(h.tag[1])
        if prev and lvl > prev + 1:
            issues.append(Issue("warning", "heading-skip", f"heading level jumps from h{prev} to h{lvl}",
                                h.text_content()[:60]))
        if not h.text_content().strip():
            issues.append(Issue("error", "empty-heading", f"empty <{h.tag}>"))
        prev = lvl

    for img in root.iter("img"):
        src = img.get("src", "")
        if src.startswith("data:"):  # an embedded image: megabytes of base64 say nothing about where it is
            src = "embedded image" + (f" ({img.get('id')})" if img.get("id") else "")
        if img.get("alt") is None:
            issues.append(Issue("error", "img-alt", "image without alt attribute (WCAG 1.1.1)", src))
        elif not img.get("alt", "").strip():
            issues.append(Issue("info", "img-alt-empty", "image marked decorative (empty alt)", src))

    for tbl in root.iter("table"):
        if not list(tbl.iter("th")):
            issues.append(Issue("warning", "table-th", "table without header cells (WCAG 1.3.1)",
                                tbl.text_content()[:60]))
        for th in tbl.iter("th"):
            if not th.get("scope"):
                issues.append(Issue("info", "th-scope", "<th> without scope", th.text_content()[:40]))
                break

    for a in root.iter("a"):
        if not a.text_content().strip() and not a.get("aria-label") and not list(a.iter("img")):
            issues.append(Issue("error", "link-text", "link without text (WCAG 2.4.4)", a.get("href", "")))
        href = a.get("href", "")
        if href.startswith("#") and root.find(f".//*[@id='{href[1:]}']") is None:
            issues.append(Issue("warning", "broken-fragment", f"link to missing id {href}"))

    ids: dict[str, int] = {}
    for el in root.iter():
        if isinstance(el.tag, str) and el.get("id"):
            ids[el.get("id")] = ids.get(el.get("id"), 0) + 1
    for i, n in ids.items():
        if n > 1:
            issues.append(Issue("error", "dup-id", f"id {i!r} used {n} times (WCAG 4.1.1)"))

    for el in root.iter():
        if isinstance(el.tag, str) and el.get("style"):
            issues.append(Issue("info", "inline-style", f"inline style on <{el.tag}>"))
            break
    for tag in ("b", "i", "font", "center"):
        if root.find(f".//{tag}") is not None:
            issues.append(Issue("info", "presentational", f"presentational <{tag}> element present"))

    pbs = [el for el in root.iter() if isinstance(el.tag, str) and el.get("role") == "doc-pagebreak"]
    if not pbs:
        issues.append(Issue("warning", "no-pagebreaks", "no page-break markers found; page numbers not citable"))
    return issues


def coverage(doc: Document, low: float = 0.6, high: float = 1.6, min_words: int = 30) -> list[Issue]:
    issues: list[Issue] = []
    for p in doc.pages:
        if p.status == "pending":
            issues.append(Issue("error", "untranscribed", f"page {p.index} has not been transcribed", f"page {p.index}"))
            continue
        if p.status == "error":
            issues.append(Issue("error", "page-error", f"page {p.index}: {p.notes}", f"page {p.index}"))
            continue
        if p.skip:
            if _words(p.draft_text) > min_words:
                issues.append(Issue("warning", "skipped-with-text",
                                    f"page {p.index} marked skip but its draft text has {_words(p.draft_text)} words",
                                    f"page {p.index}"))
            continue
        dw = _words(p.draft_text)
        try:
            # join text pieces with spaces: text_content() runs adjacent cells together ("Doe1,44360")
            hw = _words(" ".join(lhtml.fragment_fromstring(p.html or "<p></p>", create_parent="div").itertext()))
        except Exception:  # noqa: BLE001 - malformed html is reported separately
            hw = _words(re.sub(r"<[^>]+>", " ", p.html))
        if dw >= min_words:
            ratio = hw / dw if dw else 0
            if ratio < low:
                issues.append(Issue("warning", "under-coverage",
                                    f"page {p.index}: {hw} words transcribed vs {dw} in draft text (possible omission)",
                                    f"page {p.index}"))
            elif ratio > high:
                issues.append(Issue("warning", "over-coverage",
                                    f"page {p.index}: {hw} words transcribed vs {dw} in draft text (check for invented text)",
                                    f"page {p.index}"))
        if p.status == "needs_review" and p.notes:
            issues.append(Issue("info", "model-note", f"page {p.index}: {p.notes}", f"page {p.index}"))
    return issues


# ---------------------------------------------------------------- epubcheck

def find_java() -> str | None:
    j = shutil.which("java")
    if j:
        return j
    for pattern in (r"C:\Program Files\Eclipse Adoptium\*\bin\java.exe", r"C:\Program Files\Java\*\bin\java.exe",
                    "/usr/lib/jvm/*/bin/java", "/opt/homebrew/opt/openjdk*/bin/java"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def find_epubcheck() -> str | None:
    env = os.environ.get("EPUBCHECK_JAR")
    if env and Path(env).exists():
        return env
    root = Path(__file__).resolve().parents[2]
    hits = sorted(glob.glob(str(root / "tools" / "epubcheck*" / "epubcheck.jar")))
    return hits[-1] if hits else None


def epubcheck(epub_path: str | Path) -> list[Issue]:
    java, jar = find_java(), find_epubcheck()
    if not java or not jar:
        return [Issue("info", "epubcheck-unavailable",
                      "epubcheck not run (need Java and tools/epubcheck-*/epubcheck.jar or EPUBCHECK_JAR)")]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.json"
        subprocess.run([java, "-jar", jar, str(epub_path), "--json", str(out)], capture_output=True, text=True,
                       timeout=600)
        if not out.exists():
            return [Issue("error", "epubcheck-failed", "epubcheck produced no report")]
        report = json.loads(out.read_text(encoding="utf-8"))
    issues = []
    for m in report.get("messages", []):
        sev = m.get("severity", "").upper()
        level = "error" if sev in ("ERROR", "FATAL") else "warning" if sev == "WARNING" else "info"
        locs = m.get("locations") or [{}]
        loc = locs[0]
        where = f"{loc.get('path', '')}:{loc.get('line', '')}" if loc else ""
        issues.append(Issue(level, m.get("ID", "epubcheck"), m.get("message", ""), where))
    return issues
