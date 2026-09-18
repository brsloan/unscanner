"""Project files: a document's whole state as one portable JSON file, for backups and for moving
work to another machine. The file holds everything in doc.json except the machine-specific paths;
the PDF itself is not included, so an import needs the same PDF again (keep the two side by side).
"""

from __future__ import annotations

import dataclasses
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .document import Document, Page, slugify
from .sanitize import sanitize_fragment

FORMAT = "remediate-project"
VERSION = 1
SUFFIX = ".remediate.json"


class ProjectError(ValueError):
    """The file is not a usable project file for this PDF."""


class ProjectExistsError(ProjectError):
    """The PDF already has a project in the work directory and replace was not asked for."""


def project_filename(doc: Document) -> str:
    return Path(doc.source).stem + SUFFIX


def export_project(doc: Document) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "version": VERSION,
        "pdf_name": Path(doc.source).name,
        "title": doc.title,
        "author": doc.author,
        "language": doc.language,
        "pages": [asdict(p) for p in doc.pages],
    }


def _page_from(d: Any, index: int) -> Page:
    if not isinstance(d, dict):
        raise ProjectError(f"page {index} is not an object")
    known = {f.name for f in dataclasses.fields(Page)}  # a newer version's extra fields are dropped
    try:
        page = Page.from_dict({k: v for k, v in d.items() if k in known} | {"index": index})
    except TypeError as e:
        raise ProjectError(f"page {index} is malformed: {e}") from e
    page.html = sanitize_fragment(page.html or "")
    return page


def import_project(data: Any, pdf_path: str | Path, work_root: str | Path, replace: bool = False) -> Document:
    """Create the work directory for `pdf_path` from an exported project.

    Raises ProjectExistsError when that PDF already has a project here and `replace` is false.
    On a replace every page gets a version above both copies, so an editor that still has the old
    page loaded is told about the change (409) instead of overwriting the imported one.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ProjectError("not a remediate project file")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ProjectError(f"project file version {data.get('version')!r} is newer than this program understands")
    raw_pages = data.get("pages")
    if not isinstance(raw_pages, list):
        raise ProjectError("the project file has no pages")

    from .pdf import open_pdf

    pdf_path = Path(pdf_path).resolve()
    with open_pdf(pdf_path) as pdf:
        n_pages = len(pdf)
    if n_pages != len(raw_pages):
        raise ProjectError(f"the project has {len(raw_pages)} pages but {pdf_path.name} has {n_pages}; "
                           "is this the PDF the project was made from?")

    workdir = Path(work_root).resolve() / slugify(pdf_path.stem)
    pages = [_page_from(d, i + 1) for i, d in enumerate(raw_pages)]
    if Document.exists(workdir):
        if not replace:
            raise ProjectExistsError(f"{pdf_path.name} already has a project here")
        old = Document.load(workdir)
        for page, old_page in zip(pages, old.pages):
            page.version = max(page.version, old_page.version) + 1
    doc = Document(source=str(pdf_path), workdir=str(workdir), pages=pages)
    try:
        doc.set_properties(str(data.get("title") or pdf_path.stem), str(data.get("author") or ""),
                           str(data.get("language") or "en"))
    except ValueError as e:
        raise ProjectError(str(e)) from e
    doc.save()
    return doc
