"""Saving doc.json survives the brief file locks Windows hands out (another reader, a virus scanner)."""

from pathlib import Path

import pytest

from unscanner.document import Document, Page


def _doc(tmp_path: Path) -> Document:
    return Document(source=str(tmp_path / "a.pdf"), workdir=str(tmp_path / "work"), pages=[Page(index=1)])


def test_save_retries_while_doc_json_is_locked(tmp_path, monkeypatch):
    doc = _doc(tmp_path)
    doc.save()
    real_replace = Path.replace
    calls = {"n": 0}

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr("unscanner.document.time.sleep", lambda s: None)
    doc.title = "Saved on the third try"
    doc.save()

    assert calls["n"] == 3
    assert Document.load(doc.workdir).title == "Saved on the third try"


def test_save_gives_up_on_a_lock_that_never_clears(tmp_path, monkeypatch):
    doc = _doc(tmp_path)

    def locked(self, target):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr("unscanner.document.time.sleep", lambda s: None)
    with pytest.raises(PermissionError):
        doc.save()
