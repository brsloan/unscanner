"""A library's own prompt texts: files in work/prompts/ replace the editable parts of the prompts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from unscanner import mcp_server, prompts, webapp
from unscanner.pdf import new_document
from unscanner.pipeline import transcribe_pages
from unscanner.prompts import (CONTEXT, GUIDELINES, TABLE_PROMPT, build_table_prompt, guidelines, prompt_status,
                               prompt_texts, save_prompts)
from tests.test_pipeline import FakeBackend, make_pdf

OUR_CONTEXT = "CONTEXT: The Example College disability office makes accessible copies of course readings.\n"


def status(work) -> dict[str, dict]:
    return {p["name"]: p for p in prompt_status(work)}


def test_defaults_are_the_built_in_prompts(tmp_path):
    assert guidelines() == guidelines(tmp_path / "work") == GUIDELINES
    assert build_table_prompt() == build_table_prompt("", tmp_path / "work") == TABLE_PROMPT
    assert GUIDELINES.startswith(CONTEXT) and TABLE_PROMPT.startswith(CONTEXT)
    assert not any(p["customized"] or p["default_changed"] for p in prompt_status(tmp_path / "work"))


def test_edits_replace_their_part_and_keep_the_output_contract(tmp_path):
    work = tmp_path / "work"
    save_prompts(work, {"context": OUR_CONTEXT, "rules": "HTML RULES\n- Keep running headers.",
                        "table_rules": "- One row per line."})
    g = guidelines(work)
    assert g.startswith(OUR_CONTEXT) and CONTEXT not in g
    assert "OUTPUT: a single JSON object" in g and "starts_mid_paragraph" in g  # fixed, not editable
    assert g.endswith("- Keep running headers.\n") and "Drop running headers" not in g
    t = build_table_prompt("Navy $1,000", work)
    assert t.startswith(OUR_CONTEXT) and "- One row per line.\n" in t and "Dot leaders" not in t
    assert "Return only the <table> element" in t and t.rstrip().endswith("Navy $1,000")
    assert (work / "prompts" / "rules.txt").read_text(encoding="utf-8") == "HTML RULES\n- Keep running headers.\n"
    s = status(work)
    assert all(p["customized"] for p in s.values()) and s["rules"]["default"] == prompts.RULES


def test_blank_or_default_text_goes_back_to_the_default(tmp_path):
    work = tmp_path / "work"
    save_prompts(work, {"context": OUR_CONTEXT, "rules": "HTML RULES\n- x"})
    save_prompts(work, {"context": "  \n", "rules": prompts.RULES.replace("\n", "\r\n")})
    assert guidelines(work) == GUIDELINES
    assert not (work / "prompts" / "context.txt").exists() and not (work / "prompts" / "rules.txt").exists()
    assert not (work / "prompts" / "based-on.json").exists()
    with pytest.raises(ValueError, match="unknown prompt"):
        save_prompts(work, {"system": "x"})


def test_files_edited_by_hand_are_read(tmp_path):
    work = tmp_path / "work"
    (work / "prompts").mkdir(parents=True)
    (work / "prompts" / "context.txt").write_bytes(b"\xef\xbb\xbfCONTEXT: Notepad wrote this.\r\n\r\n")
    (work / "prompts" / "rules.txt").write_text("   \n", encoding="utf-8")  # blank: the default
    assert prompt_texts(work)["context"] == "CONTEXT: Notepad wrote this.\n"
    assert prompt_texts(work)["rules"] == prompts.RULES
    s = status(work)
    assert s["context"]["customized"] and not s["context"]["default_changed"]  # no record of its origin


def test_an_improved_default_is_flagged_until_the_text_is_edited(tmp_path, monkeypatch):
    work = tmp_path / "work"
    save_prompts(work, {"rules": "HTML RULES\n- ours"})
    assert not status(work)["rules"]["default_changed"]
    monkeypatch.setitem(prompts.PROMPT_DEFAULTS, "rules", prompts.RULES + "- a new rule\n")  # an update ships
    assert status(work)["rules"]["default_changed"]
    save_prompts(work, {"rules": "HTML RULES\n- ours"})  # saving it unchanged is not a review
    assert status(work)["rules"]["default_changed"]
    save_prompts(work, {"rules": "HTML RULES\n- ours\n- and the new rule"})
    assert not status(work)["rules"]["default_changed"]


def test_batch_runs_send_the_edited_guidelines(tmp_path):
    seen = []

    class Recording(FakeBackend):
        def transcribe(self, image_png, user_prompt, system=None):
            seen.append(system)
            return super().transcribe(image_png, user_prompt)

    doc = new_document(make_pdf(tmp_path / "a.pdf"), tmp_path / "work", title="A")
    save_prompts(tmp_path / "work", {"context": OUR_CONTEXT})
    assert transcribe_pages(doc, Recording(), [1, 2], workers=1)["done"] == 2
    assert seen == [guidelines(tmp_path / "work")] * 2 and seen[0].startswith(OUR_CONTEXT)


def test_prompts_api_and_table_route(tmp_path, monkeypatch):
    seen = {}

    class TableBackend(FakeBackend):
        def describe_image(self, image_png, prompt, max_tokens=None):
            seen["prompt"] = prompt
            return "<table><tr><td>1</td></tr></table>"

    monkeypatch.setattr("unscanner.backends.make_backend", lambda *a, **k: TableBackend())
    with TestClient(webapp.create_app(tmp_path / "work", tmp_path / "out")) as c:
        r = c.get("/api/prompts").json()["prompts"]
        assert [p["name"] for p in r] == ["context", "rules", "table_rules"] and not any(p["customized"] for p in r)
        r = c.put("/api/prompts", json={"context": OUR_CONTEXT, "table_rules": "- One row per line."}).json()["prompts"]
        assert [p["customized"] for p in r] == [True, False, True]
        assert r[0]["file"].endswith("context.txt") and r[0]["text"] == OUR_CONTEXT
        assert c.get("/api/guidelines").json()["guidelines"].startswith(OUR_CONTEXT)
        assert c.put("/api/prompts", json={"system": "x"}).status_code == 400

        doc_id = c.post("/api/documents", json={"pdf_path": str(make_pdf(tmp_path / "t.pdf"))}).json()["doc_id"]
        assert c.post(f"/api/documents/{doc_id}/pages/1/table", json={"bbox": [0, 0, 1000, 1000]}).status_code == 200
        assert seen["prompt"].startswith(OUR_CONTEXT) and "- One row per line." in seen["prompt"]

        c.put("/api/prompts", json={"context": "", "table_rules": ""})
        assert not any(p["customized"] for p in c.get("/api/prompts").json()["prompts"])


@pytest.mark.anyio
async def test_agents_get_the_edited_guidelines(tmp_path):
    from mcp.client.client import Client

    work = tmp_path / "work"
    save_prompts(work, {"rules": "HTML RULES\n- House rule for agents."})
    mcp_server.configure(work, tmp_path / "out")
    async with Client(mcp_server.server) as agent:
        r = await agent.call_tool("get_guidelines", {})
    text = r.content[0].text
    assert text.startswith(CONTEXT) and text.rstrip().endswith("- House rule for agents.")
