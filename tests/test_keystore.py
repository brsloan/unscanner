"""API keys: OS credential store when there is one, work/settings.json otherwise; never sent to the browser."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import remediate.backends as backends
from remediate import keystore, webapp
from tests.test_pipeline import make_pdf


@pytest.fixture
def client(tmp_path):
    app = webapp.create_app(tmp_path / "work", tmp_path / "out", mount_mcp=False)
    with TestClient(app) as c:
        yield c


def stored(tmp_path) -> dict:
    return json.loads((tmp_path / "work" / "settings.json").read_text(encoding="utf-8"))


def key_reaching_backend(client, tmp_path, monkeypatch) -> dict:
    seen = {}

    def fake_make_backend(name=None, model=None, **kw):
        seen.update(kw, name=name)
        raise backends.BackendError("stop here")

    monkeypatch.setattr(backends, "make_backend", fake_make_backend)
    pdf = make_pdf(tmp_path / "key sample.pdf")
    doc_id = client.post("/api/documents", json={"pdf_path": str(pdf)}).json()["doc_id"]
    client.post(f"/api/documents/{doc_id}/transcribe", json={"pages": "1"})
    return seen


def test_key_goes_to_the_credential_store_not_the_file(client, tmp_path, monkeypatch, memory_keyring):
    r = client.put("/api/settings", json={"backend": "openai", "openai_api_key": "sk-secret"}).json()
    assert r["openai_api_key"] == "" and r["openai_api_key_set"] is True and r["key_storage"] == "keyring"
    assert stored(tmp_path)["openai_api_key"] == ""
    assert memory_keyring.items == {("remediate", "openai_api_key"): "sk-secret"}
    assert key_reaching_backend(client, tmp_path, monkeypatch)["api_key"] == "sk-secret"


def test_empty_field_keeps_the_key_and_null_forgets_it(client, memory_keyring):
    client.put("/api/settings", json={"anthropic_api_key": "sk-ant"})
    r = client.put("/api/settings", json={"anthropic_api_key": "", "workers": 3}).json()
    assert r["anthropic_api_key_set"] is True and r["workers"] == 3
    r = client.put("/api/settings", json={"anthropic_api_key": None}).json()
    assert r["anthropic_api_key_set"] is False and memory_keyring.items == {}


def test_plaintext_key_in_the_file_is_migrated(client, tmp_path, memory_keyring):
    path = tmp_path / "work" / "settings.json"
    path.write_text(json.dumps({"openai_api_key": "sk-old", "workers": 2}), encoding="utf-8")
    r = client.get("/api/settings").json()
    assert r["openai_api_key"] == "" and r["openai_api_key_set"] is True and r["workers"] == 2
    assert stored(tmp_path)["openai_api_key"] == ""
    assert keystore.get_secret("openai_api_key") == "sk-old"


def test_without_a_credential_store_the_key_stays_in_the_file(client, tmp_path, monkeypatch):
    r = client.put("/api/settings", json={"backend": "openai", "openai_api_key": "sk-file"}).json()
    assert r["openai_api_key"] == "" and r["openai_api_key_set"] is True and r["key_storage"] == "file"
    assert stored(tmp_path)["openai_api_key"] == "sk-file"
    assert client.get("/api/settings").json()["openai_api_key"] == ""  # still never sent to the browser
    assert key_reaching_backend(client, tmp_path, monkeypatch)["api_key"] == "sk-file"


def test_store_that_fails_to_write_falls_back_to_the_file(client, tmp_path, memory_keyring, monkeypatch):
    def broken(*a):
        raise RuntimeError("store is locked")

    monkeypatch.setattr(memory_keyring, "set_password", broken)
    client.put("/api/settings", json={"openai_api_key": "sk-x"})
    assert stored(tmp_path)["openai_api_key"] == "sk-x"
