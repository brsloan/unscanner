"""Projects on this computer: a PDF is opened where it is and known by its content, the page cache
lives apart from the work and is trimmed, and a project can be located again or removed."""

from __future__ import annotations

import json
import shutil
import time

from fastapi.testclient import TestClient

from unscanner import desktop, pdf, webapp
from unscanner.document import Document, doc_id_for, fingerprint, project_dirs
from unscanner.pdf import new_document, trim_cache
from tests.test_pipeline import make_pdf


def make_client(root):
    return TestClient(webapp.create_app(root / "work", root / "out"))


def other_pdf(path, n_pages=2):
    """A PDF that is a different file from make_pdf's: other text, and its own page count."""
    import pymupdf

    doc = pymupdf.open()
    for i in range(n_pages):
        doc.new_page(width=400, height=600).insert_text((40, 60), f"Another document, page {i + 1}", fontsize=11)
    doc.save(str(path))
    return path


def test_fingerprint_is_the_content_not_the_name(tmp_path):
    a = make_pdf(tmp_path / "reading.pdf")
    same = tmp_path / "copy" / "reading (1).pdf"
    same.parent.mkdir()
    shutil.copy(a, same)
    (tmp_path / "other").mkdir()
    other = other_pdf(tmp_path / "other" / "reading.pdf")
    assert fingerprint(a) == fingerprint(same)
    assert fingerprint(a) != fingerprint(other)
    assert doc_id_for(a, fingerprint(a)) == "reading-" + fingerprint(a)[:8]


def test_two_pdfs_with_one_name_get_two_projects(tmp_path):
    (tmp_path / "hist").mkdir(); (tmp_path / "engl").mkdir()
    a = make_pdf(tmp_path / "hist" / "reading.pdf")
    b = other_pdf(tmp_path / "engl" / "reading.pdf")
    da = new_document(a, tmp_path / "work", title="History")
    db = new_document(b, tmp_path / "work", title="English")
    assert da.workdir != db.workdir and len(da.pages) == 3 and len(db.pages) == 2
    assert Document.load(da.workdir).title == "History"  # not overwritten by the second open


def test_a_renamed_or_moved_pdf_finds_its_project(tmp_path):
    a = make_pdf(tmp_path / "before.pdf")
    doc = new_document(a, tmp_path / "work", title="Kept")
    moved = tmp_path / "elsewhere" / "after.pdf"
    moved.parent.mkdir()
    a.rename(moved)
    again = new_document(moved, tmp_path / "work")
    assert again.workdir == doc.workdir and again.title == "Kept" and again.source == str(moved.resolve())
    assert len(project_dirs(tmp_path / "work")) == 1


def test_a_project_from_before_fingerprints_is_adopted(tmp_path):
    a = make_pdf(tmp_path / "old reading.pdf")
    legacy = tmp_path / "work" / "old-reading"  # named after the PDF alone, as earlier versions did
    doc = Document(source=str(a), workdir=str(legacy), title="Old", pages=[])
    doc.save()
    assert doc.fingerprint == ""
    again = new_document(a, tmp_path / "work")
    assert again.workdir == str(legacy) and again.fingerprint == fingerprint(a) and again.title == "Old"
    # a different PDF with the same name, while the legacy project's own PDF is still there: a new project
    (tmp_path / "x").mkdir()
    other = other_pdf(tmp_path / "x" / "old reading.pdf")
    assert new_document(other, tmp_path / "work").workdir != str(legacy)


def test_cache_root_keeps_renders_out_of_the_work_folder(tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "cached.pdf")
    doc = new_document(a, tmp_path / "work")
    stale = tmp_path / "work" / doc.workdir.split("\\")[-1].split("/")[-1] / "pages"
    stale.mkdir()
    (stale / "p001.png").write_bytes(b"old")  # a cache an earlier version left in the work folder
    monkeypatch.setattr(pdf, "cache_root", tmp_path / "cache")
    png = pdf.cached_page_png(doc, 1)
    assert png.parent == tmp_path / "cache" / stale.parent.name and png.stat().st_size > 100
    assert not stale.exists()
    pdf.drop_cache(doc)
    assert not png.exists()


def test_trim_cache_drops_the_oldest_projects_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "cache_root", tmp_path / "cache")
    for name, age in (("old", 300), ("mid", 200), ("new", 100)):
        d = tmp_path / "cache" / name
        d.mkdir(parents=True)
        (d / "p001.png").write_bytes(b"x" * 100)
        t = time.time() - age
        import os
        os.utime(d / "p001.png", (t, t))
    assert trim_cache(tmp_path / "work", max_bytes=250) == ["old"]
    assert trim_cache(tmp_path / "work", keep="mid", max_bytes=150) == ["new"]
    assert trim_cache(tmp_path / "work", max_bytes=1000) == []


def test_list_remove_and_locate(tmp_path):
    (tmp_path / "scans").mkdir()
    first = make_pdf(tmp_path / "scans" / "first.pdf")
    second = other_pdf(tmp_path / "scans" / "second.pdf")
    with make_client(tmp_path) as c:
        id1 = c.post("/api/documents", json={"pdf_path": str(first)}).json()["doc_id"]
        time.sleep(1.1)  # opened_at is to the second
        id2 = c.post("/api/documents", json={"pdf_path": str(second)}).json()["doc_id"]
        docs = c.get("/api/documents").json()
        assert [d["doc_id"] for d in docs] == [id2, id1] and all(d["source_found"] for d in docs)
        assert c.get(f"/api/documents/{id1}/pages/1/image").status_code == 200

        # the PDF is delivered and deleted: the project stays, readable, and says so
        moved = tmp_path / "delivered.pdf"
        first.rename(moved)
        docs = {d["doc_id"]: d for d in c.get("/api/documents").json()}
        assert docs[id1]["source_found"] is False and docs[id2]["source_found"] is True
        assert c.get(f"/api/documents/{id1}/pages/1").status_code == 200
        r = c.get(f"/api/documents/{id1}/pages/1/image")
        assert r.status_code == 404 and "Locate" in r.json()["detail"]

        # Locate: the wrong file is refused, the right one relinks
        r = c.post(f"/api/documents/{id1}/source", json={"pdf_path": str(second)})
        assert r.status_code == 400 and "not the PDF" in r.json()["detail"]
        r = c.post(f"/api/documents/{id1}/source", json={"pdf_path": str(moved)})
        assert r.status_code == 200 and r.json()["source_found"] and r.json()["source"] == str(moved.resolve())
        assert c.get(f"/api/documents/{id1}/pages/1/image").status_code == 200

        # Remove: work folder and cache go, the PDF stays
        cache = pdf.cache_dir(Document.load(tmp_path / "work" / id1))
        assert cache.is_dir()
        assert c.delete(f"/api/documents/{id1}").json() == {"removed": id1}
        assert not (tmp_path / "work" / id1).exists() and not cache.exists() and moved.exists()
        assert [d["doc_id"] for d in c.get("/api/documents").json()] == [id2]
        assert c.delete(f"/api/documents/{id1}").status_code == 404


def test_removing_an_uploaded_pdf_removes_the_copy_too(tmp_path):
    src = make_pdf(tmp_path / "uploaded.pdf")
    with make_client(tmp_path) as c:
        with src.open("rb") as fh:
            r = c.post("/api/documents/upload", files={"file": ("uploaded.pdf", fh, "application/pdf")})
        assert r.status_code == 200, r.text
        copy = tmp_path / "work" / "_inbox" / "uploaded.pdf"
        assert copy.exists() and r.json()["source"] == str(copy.resolve())
        c.delete(f"/api/documents/{r.json()['doc_id']}")
        assert not copy.exists() and src.exists()


def test_pick_file_needs_the_window(tmp_path, monkeypatch):
    with make_client(tmp_path) as c:
        assert c.get("/api/app").json()["window"] is False
        assert c.post("/api/pick-file", json={"kind": "pdf"}).status_code == 409
        assert c.post("/api/pick-file", json={"kind": "exe"}).status_code == 400

        class FakeWindow:
            def create_file_dialog(self, dialog_type, directory, allow_multiple, file_types):
                assert file_types == desktop.FILE_TYPES["pdf"] and not allow_multiple
                return (r"C:\scans\chosen.pdf",)

        monkeypatch.setattr(desktop, "window", FakeWindow())
        import types

        monkeypatch.setitem(__import__("sys").modules, "webview", types.SimpleNamespace(OPEN_DIALOG=10))
        assert c.get("/api/app").json()["window"] is True
        assert c.post("/api/pick-file", json={"kind": "pdf"}).json() == {"path": r"C:\scans\chosen.pdf"}
        FakeWindow.create_file_dialog = lambda self, *a, **k: None  # cancelled
        assert c.post("/api/pick-file", json={"kind": "project"}).json() == {"path": None}


def test_import_from_a_path_on_this_computer(tmp_path):
    a = make_pdf(tmp_path / "local.pdf")
    with make_client(tmp_path) as c:
        doc_id = c.post("/api/documents", json={"pdf_path": str(a), "title": "Local"}).json()["doc_id"]
        c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>Kept.</p>"})
        project_file = tmp_path / "local.unscanner.json"
        project_file.write_bytes(c.get(f"/api/documents/{doc_id}/export").content)
        assert json.loads(project_file.read_text())["fingerprint"] == fingerprint(a)
        c.delete(f"/api/documents/{doc_id}")
        r = c.post("/api/projects/import", data={"project_path": str(project_file), "pdf_path": str(a)})
        assert r.status_code == 200, r.text
        assert c.get(f"/api/documents/{r.json()['doc_id']}/pages/1").json()["html"] == "<p>Kept.</p>"
        assert c.post("/api/projects/import", data={"pdf_path": str(a)}).status_code == 400
        r = c.post("/api/projects/import", data={"project_path": str(tmp_path / "nope.json")})
        assert r.status_code == 400 and "cannot read" in r.json()["detail"]


def test_output_folder_is_named_like_the_work_folder(tmp_path):
    a = make_pdf(tmp_path / "built.pdf")
    with make_client(tmp_path) as c:
        doc_id = c.post("/api/documents", json={"pdf_path": str(a), "title": "Built"}).json()["doc_id"]
        c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<h1>Built</h1><p>x</p>"})
        r = c.post(f"/api/documents/{doc_id}/build", json={"epub": False})
        assert r.status_code == 200, r.text
        assert (tmp_path / "out" / doc_id / "built.html").exists()


def test_a_title_or_author_made_by_software_is_not_used(tmp_path):
    import pymupdf

    from unscanner.pdf import usable_author, usable_title

    for made_up in ["Microsoft Word - $ASQ102201_supp_undefined_5CB76878-AF29-11E0-8D1D-9A29D352ABB1.doc",
                    "Microsoft PowerPoint - Week 3", "chapter3.docx", "Smith_2011_ch3", "Untitled", "Document1",
                    "Scanned Document", "scan 0001", "reading 5cb76878af2911e08d1d", "  "]:
        assert usable_title(made_up) == "", made_up
    for real in ["The Sumter Slave Bounty", "1984", "Word and Object", "Chapter 3: Reconstruction"]:
        assert usable_title(real) == real
    for made_up in ["svc-activepdf", "Administrator", "Microsoft Office User", "owner"]:
        assert usable_author(made_up) == "", made_up
    assert usable_author(" Eric Foner ") == "Eric Foner"

    path = make_pdf(tmp_path / "Sumter Slave Bounty.pdf")
    with pymupdf.open(str(path)) as p:
        p.set_metadata({"title": "Microsoft Word - $ASQ102201_supp.doc", "author": "svc-activepdf"})
        p.saveIncr()
    doc = new_document(path, tmp_path / "work")
    assert doc.title == "Sumter Slave Bounty" and doc.author == ""
