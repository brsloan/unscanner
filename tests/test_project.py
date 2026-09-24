"""Export project / import project: moving a document's state between machines as one JSON file."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from unscanner import cli, webapp
from unscanner.document import Document, project_dirs
from unscanner.project import ProjectError, import_project
from tests.test_pipeline import make_pdf


def make_client(root):
    return TestClient(webapp.create_app(root / "work", root / "out"))


def exported(client, pdf, html="<p>First page.</p>"):
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf), "title": "Moved", "author": "A. Author"}).json()["doc_id"]
    r = client.put(f"/api/documents/{doc_id}/pages/1", json={"html": html, "label": "37", "notes": "check"})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/documents/{doc_id}/export")
    assert r.status_code == 200
    return doc_id, r


def test_export_then_import_on_another_machine(tmp_path):
    (tmp_path / "a").mkdir()
    pdf = make_pdf(tmp_path / "a" / "moving sample.pdf")
    with make_client(tmp_path / "a") as a:
        doc_id, r = exported(a, pdf)
    assert "moving%20sample.unscanner.json" in r.headers["content-disposition"]
    data = r.json()
    assert data["format"] == "unscanner-project" and data["pdf_name"] == "moving sample.pdf"
    assert "source" not in data and "workdir" not in data  # nothing machine-specific in the file

    with make_client(tmp_path / "b") as b:
        files = {"project_file": ("p.unscanner.json", r.content, "application/json"),
                 "pdf": ("moving sample.pdf", pdf.read_bytes(), "application/pdf")}
        r2 = b.post("/api/projects/import", files=files)
        assert r2.status_code == 200, r2.text
        assert r2.json()["doc_id"] == doc_id and r2.json()["title"] == "Moved" and r2.json()["author"] == "A. Author"
        page = b.get(f"/api/documents/{doc_id}/pages/1").json()
        assert page["html"] == "<p>First page.</p>" and page["label"] == "37" and page["notes"] == "check"
        assert page["changed_by"] == "editor"
        img = b.get(f"/api/documents/{doc_id}/pages/1/image")
        assert img.status_code == 200  # the uploaded PDF is the new source
        assert str(tmp_path / "b") in Document.load(tmp_path / "b" / "work" / doc_id).source


def test_import_over_existing_project_needs_replace(tmp_path):
    pdf = make_pdf(tmp_path / "replace sample.pdf")
    with make_client(tmp_path) as c:
        doc_id, r = exported(c, pdf, "<p>Exported text.</p>")
        loaded = c.get(f"/api/documents/{doc_id}/pages/1").json()["version"]
        c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>Later text.</p>", "version": loaded})

        files = {"project_file": ("p.json", r.content, "application/json")}  # no PDF: the one opened here is used
        assert c.post("/api/projects/import", files=files).status_code == 409
        assert "Later" in c.get(f"/api/documents/{doc_id}/pages/1").json()["html"]

        r2 = c.post("/api/projects/import", files=files, data={"replace": "true"})
        assert r2.status_code == 200, r2.text
        page = c.get(f"/api/documents/{doc_id}/pages/1").json()
        assert page["html"] == "<p>Exported text.</p>"
        # an editor that still holds the replaced page gets a conflict, not a silent overwrite
        assert page["version"] > loaded + 1
        stale = c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>stale</p>", "version": loaded + 1})
        assert stale.status_code == 409


def test_import_rejects_bad_files(tmp_path):
    pdf = make_pdf(tmp_path / "bad sample.pdf")
    with make_client(tmp_path) as c:
        _, r = exported(c, pdf)
        data = r.json()
        post = lambda body, **kw: c.post("/api/projects/import", data={"replace": "true", **kw},  # noqa: E731
                                         files={"project_file": ("p.json", body, "application/json")})
        assert post(b"not json").status_code == 400
        assert post(json.dumps({"pages": []}).encode()).status_code == 400
        assert post(json.dumps({**data, "version": 99}).encode()).status_code == 400
        short = post(json.dumps({**data, "pages": data["pages"][:1]}).encode())
        assert short.status_code == 400 and "pages" in short.json()["detail"]
        # the fingerprint finds the PDF opened here whatever the file was called; without it, the name has to
        renamed = post(json.dumps({**data, "pdf_name": "never seen.pdf"}).encode())
        assert renamed.status_code == 200, renamed.text
        unknown = post(json.dumps({**data, "pdf_name": "never seen.pdf", "fingerprint": "0" * 16}).encode())
        assert unknown.status_code == 400 and "never seen.pdf" in unknown.json()["detail"]
        # the PDF given is not the one the project came from
        from tests.test_projects import other_pdf

        other = other_pdf(tmp_path / "other.pdf", n_pages=3)
        wrong = post(r.content, pdf_path=str(other))
        assert wrong.status_code == 400 and "not the PDF" in wrong.json()["detail"]
        assert post(r.content, pdf_path=str(tmp_path / "missing.pdf")).status_code == 400


def test_import_sanitizes_html_and_ignores_unknown_fields(tmp_path):
    pdf = make_pdf(tmp_path / "hand edited.pdf")
    pages = [{"index": 9, "html": '<p style="color:red" onclick="x()">Hi</p><script>x()</script>', "from_the_future": 1},
             {"html": ""}, {"html": ""}]
    doc = import_project({"format": "unscanner-project", "version": 1, "title": "T", "pages": pages}, pdf, tmp_path / "work")
    assert doc.pages[0].html == "<p>Hi</p>" and [p.index for p in doc.pages] == [1, 2, 3]
    with pytest.raises(ProjectError):
        import_project({"format": "unscanner-project", "version": 1, "pages": pages, "language": "not a code"},
                       pdf, tmp_path / "work", replace=True)


def test_cli_export_writes_next_to_the_pdf_and_import_finds_it(tmp_path, capsys):
    pdf = make_pdf(tmp_path / "cli sample.pdf")
    cli.main(["--work", str(tmp_path / "w1"), "open", str(pdf), "--title", "CLI"])
    cli.main(["--work", str(tmp_path / "w1"), "export", str(pdf)])
    project_file = tmp_path / "cli sample.unscanner.json"
    assert project_file.exists()
    cli.main(["--work", str(tmp_path / "w2"), "import", str(project_file)])
    [wd] = project_dirs(tmp_path / "w2")
    assert wd.name.startswith("cli-sample-") and Document.load(wd).title == "CLI"
    with pytest.raises(SystemExit):
        cli.main(["--work", str(tmp_path / "w2"), "import", str(project_file)])


def test_project_file_from_the_old_program_name_still_imports(tmp_path):
    pdf = make_pdf(tmp_path / "old name.pdf")
    pages = [{"html": "<p>Hi</p>"}, {"html": ""}, {"html": ""}]
    doc = import_project({"format": "remediate-project", "version": 1, "pages": pages}, pdf, tmp_path / "work")
    assert doc.pages[0].html == "<p>Hi</p>"
