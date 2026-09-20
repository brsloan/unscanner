"""Command line interface.

  unscanner open   <pdf> [--work work] [--title T] [--author A] [--lang en]
  unscanner transcribe <pdf|workdir> [--pages 1-5] [--backend anthropic|openai] [--model M] [--force]
                       [--fallback anthropic|openai] [--fallback-model M] [--no-title]
  unscanner build  <pdf|workdir> [--out out] [--no-epub]
  unscanner validate <pdf|workdir>
  unscanner status <pdf|workdir>
  unscanner run    <pdf> ...      (open + transcribe + build + validate)
  unscanner page   <pdf|workdir> N            print a page's stored html
  unscanner set-page <pdf|workdir> N file.html [--label L] [--starts-mid] [--ends-mid]
  unscanner export <pdf|workdir> [-o file]    write the project file (default: next to the PDF)
  unscanner import <file.unscanner.json> [--pdf P] [--replace]
  unscanner serve  [--work work]  (MCP server over stdio)
  unscanner ui     [--port 8765] [--no-browser]   (local web UI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .document import Document, parse_page_range, slugify


def _resolve_doc(target: str, work: str) -> Document:
    p = Path(target)
    if p.is_dir() and Document.exists(p):
        return Document.load(p)
    if p.suffix.lower() == ".pdf":
        wd = Path(work) / slugify(p.stem)
        if Document.exists(wd):
            return Document.load(wd)
        from .pdf import new_document

        return new_document(p, work)
    raise SystemExit(f"not a PDF or work directory: {target}")


def cmd_open(args) -> None:
    from .pdf import new_document

    doc = new_document(args.pdf, args.work, title=args.title or "", author=args.author or "", language=args.lang)
    print(json.dumps(doc.summary(), indent=1))


def cmd_status(args) -> None:
    doc = _resolve_doc(args.target, args.work)
    print(json.dumps(doc.summary(), indent=1))


def cmd_transcribe(args) -> None:
    from .backends import make_backend
    from .pipeline import transcribe_pages

    doc = _resolve_doc(args.target, args.work)
    kwargs = {}
    if args.backend in (None, "anthropic") and args.effort:
        kwargs["effort"] = args.effort
    if args.backend == "openai" and args.think:
        kwargs["disable_thinking"] = False
    if args.max_tokens:
        kwargs["max_tokens"] = args.max_tokens
    backend = make_backend(args.backend, args.model, **kwargs)
    fallback = None
    if args.fallback and args.fallback != backend.name:
        fallback = make_backend(args.fallback, args.fallback_model)
    pages = parse_page_range(args.pages, len(doc.pages))

    def progress(page, status):
        print(f"page {page.index:4d}  {status:13s} label={page.label!r}", file=sys.stderr)

    summary = transcribe_pages(doc, backend, pages, force=args.force, workers=args.workers, on_progress=progress,
                               fallback=fallback, send_title=not args.no_title)
    print(json.dumps(summary, indent=1))


def built_html(out_dir: Path) -> Path | None:
    """The HTML file of the last build: `<title>.html`, the only .html file a build leaves in the folder."""
    files = sorted(out_dir.glob("*.html"), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


def _build(doc: Document, out_root: str, epub: bool = True) -> dict:
    from .assemble import assemble, export_style, image_options, load_export_settings
    from .epub import build_epub

    out_dir = Path(out_root) / slugify(Path(doc.source).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Indent, justification and page numbers per format, and how the figure files are written: the
    # Export section of the UI's settings.
    settings = load_export_settings(Path(doc.workdir).parent)
    a = assemble(doc, out_dir, image_options(settings))
    # Named after the title, like the EPUB, so the file says what it is once it leaves this folder.
    html_path = out_dir / (slugify(doc.title) + ".html")
    for old in out_dir.glob("*.html"):  # an earlier build's index.html, or the file of a former title
        if old != html_path:
            old.unlink()
    html_path.write_text(a.html(export_style("html", settings)), encoding="utf-8")
    result = {"html": str(html_path), "figures": len(a.figures), "warnings": a.warnings}
    if epub:
        epub_path = build_epub(a, out_dir / (slugify(doc.title) + ".epub"), author=doc.author,
                               source=Path(doc.source).name, style=export_style("epub", settings))
        result["epub"] = str(epub_path)
    return result


def cmd_build(args) -> None:
    doc = _resolve_doc(args.target, args.work)
    print(json.dumps(_build(doc, args.out, epub=not args.no_epub), indent=1))


def _validate(doc: Document, out_root: str, run_epubcheck: bool = True) -> dict:
    from .assemble import export_style, load_export_settings
    from .validate import coverage, epubcheck, validate_html

    out_dir = Path(out_root) / slugify(Path(doc.source).stem)
    html_path = built_html(out_dir)
    issues = []
    if html_path:
        issues += validate_html(html_path.read_text(encoding="utf-8"))
        if not export_style("html", load_export_settings(Path(doc.workdir).parent)).page_numbers:
            issues = [i for i in issues if i.code != "no-pagebreaks"]  # left out on purpose
    else:
        issues.append({"severity": "error", "code": "no-build", "message": "run build first", "location": ""})
    issues = [i.to_dict() if hasattr(i, "to_dict") else i for i in issues]
    issues += [i.to_dict() for i in coverage(doc)]
    if run_epubcheck:
        epubs = sorted(out_dir.glob("*.epub"))
        if epubs:
            issues += [i.to_dict() for i in epubcheck(epubs[-1])]
    counts = {}
    for i in issues:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    return {"counts": counts, "issues": issues}


def cmd_validate(args) -> None:
    doc = _resolve_doc(args.target, args.work)
    print(json.dumps(_validate(doc, args.out, run_epubcheck=not args.no_epubcheck), indent=1))


def cmd_run(args) -> None:
    cmd_open(args)
    args.target = args.pdf
    args.force = False
    cmd_transcribe(args)
    args.no_epub = False
    cmd_build(args)
    args.no_epubcheck = False
    cmd_validate(args)


def cmd_page(args) -> None:
    doc = _resolve_doc(args.target, args.work)
    p = doc.page(args.page)
    print(json.dumps({k: v for k, v in p.__dict__.items() if k not in ("html", "draft_text")}, indent=1, default=str))
    print(p.html)


def cmd_set_page(args) -> None:
    from .pipeline import apply_result

    doc = _resolve_doc(args.target, args.work)
    html = Path(args.file).read_text(encoding="utf-8")
    apply_result(doc.page(args.page), {"label": args.label, "skip": False, "starts_mid_paragraph": args.starts_mid,
                                       "ends_mid_paragraph": args.ends_mid, "html": html, "figures": [],
                                       "notes": ""}, model="manual")
    doc.save()
    print(json.dumps(doc.summary(), indent=1))


def cmd_export(args) -> None:
    from .project import export_project, project_filename

    doc = _resolve_doc(args.target, args.work)
    dest = Path(args.output) if args.output else Path(doc.source).with_name(project_filename(doc))
    dest.write_text(json.dumps(export_project(doc), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {dest}")


def cmd_import(args) -> None:
    from .project import SUFFIX, ProjectError, import_project

    pf = Path(args.project)
    pdf = Path(args.pdf) if args.pdf else pf.with_name(pf.name.removesuffix(SUFFIX) + ".pdf")
    if not pdf.is_file():
        raise SystemExit(f"PDF not found: {pdf} (pass --pdf)")
    try:
        doc = import_project(json.loads(pf.read_text(encoding="utf-8-sig")), pdf, args.work, replace=args.replace)
    except (ProjectError, json.JSONDecodeError) as e:
        raise SystemExit(f"import failed: {e}" + ("" if args.replace else " (use --replace to overwrite)")) from e
    print(json.dumps(doc.summary(), indent=1))


def cmd_serve(args) -> None:
    import os

    os.environ.setdefault("UNSCANNER_WORK_DIR", str(Path(args.work).resolve()))
    os.environ.setdefault("UNSCANNER_OUT_DIR", str(Path(args.out).resolve()))
    from .mcp_server import server

    server.run(transport="stdio")


def cmd_ui(args) -> None:
    from .webapp import serve

    serve(args.work, args.out, host=args.host, port=args.port, open_browser=not args.no_browser)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="unscanner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work", help="work directory root (default: ./work)")
    ap.add_argument("--out", default="out", help="output directory root (default: ./out)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_model_args(p):
        p.add_argument("--pages", default="all", help="e.g. 1-5,9 (default all)")
        p.add_argument("--backend", choices=["anthropic", "openai"], default=None)
        p.add_argument("--model", default=None)
        p.add_argument("--effort", default=None, help="anthropic only: low|medium|high (default medium)")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--fallback", choices=["anthropic", "openai"], default=None,
                       help="backend to retry a page on when the main backend refuses it")
        p.add_argument("--fallback-model", default=None, help="model id for --fallback")
        p.add_argument("--no-title", action="store_true",
                       help="leave the document title out of the prompt (helps with refusals of well-known works)")
        p.add_argument("--think", action="store_true",
                       help="openai only: let a reasoning model think first (off by default: it eats the output budget)")
        p.add_argument("--max-tokens", type=int, default=None,
                       help="output budget per page; raise it when dense table pages come back truncated")

    p = sub.add_parser("open"); p.add_argument("pdf"); p.add_argument("--title"); p.add_argument("--author")
    p.add_argument("--lang", default="en"); p.set_defaults(func=cmd_open)
    p = sub.add_parser("status"); p.add_argument("target"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("transcribe"); p.add_argument("target"); add_model_args(p)
    p.add_argument("--force", action="store_true", help="redo pages that are already done"); p.set_defaults(func=cmd_transcribe)
    p = sub.add_parser("build"); p.add_argument("target"); p.add_argument("--no-epub", action="store_true")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("validate"); p.add_argument("target"); p.add_argument("--no-epubcheck", action="store_true")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("run"); p.add_argument("pdf"); p.add_argument("--title"); p.add_argument("--author")
    p.add_argument("--lang", default="en"); add_model_args(p); p.set_defaults(func=cmd_run)
    p = sub.add_parser("page"); p.add_argument("target"); p.add_argument("page", type=int); p.set_defaults(func=cmd_page)
    p = sub.add_parser("set-page"); p.add_argument("target"); p.add_argument("page", type=int); p.add_argument("file")
    p.add_argument("--label"); p.add_argument("--starts-mid", action="store_true"); p.add_argument("--ends-mid", action="store_true")
    p.set_defaults(func=cmd_set_page)
    p = sub.add_parser("export"); p.add_argument("target"); p.add_argument("-o", "--output", help="default: next to the PDF")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("import"); p.add_argument("project"); p.add_argument("--pdf", help="default: the PDF next to the project file")
    p.add_argument("--replace", action="store_true", help="overwrite the project this PDF already has")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("serve"); p.set_defaults(func=cmd_serve)
    p = sub.add_parser("ui"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true"); p.set_defaults(func=cmd_ui)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
