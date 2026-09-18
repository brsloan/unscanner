"""Collaboration between the web UI and an agent on the MCP server: shared session, versioning,
conflict detection, show_page, per-run instructions, and the MCP endpoint mounted in the web app."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from unscanner import mcp_server, webapp
from unscanner.pipeline import page_prompt
from unscanner.session import Session
from tests.test_pipeline import make_pdf


@pytest.fixture
def env(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    app = webapp.create_app(work, out, mount_mcp=False)
    with TestClient(app) as c:
        pdf = make_pdf(tmp_path / "collab.pdf")
        doc_id = c.post("/api/documents", json={"pdf_path": str(pdf), "title": "Collab"}).json()["doc_id"]
        yield c, doc_id, work


def test_versioning_and_conflict(env):
    c, doc_id, _ = env
    p = c.get(f"/api/documents/{doc_id}/pages/1").json()
    assert p["version"] == 0
    r = c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>one</p>", "version": 0})
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["changed_by"] == "editor"
    # A save based on the stale version is refused...
    r = c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>stale</p>", "version": 0})
    assert r.status_code == 409 and "editor" in r.json()["detail"]
    # ...an explicit overwrite (no version) or the current version is accepted.
    assert c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>two</p>", "version": 1}).status_code == 200
    assert c.put(f"/api/documents/{doc_id}/pages/1", json={"html": "<p>three</p>"}).json()["version"] == 3


@pytest.mark.anyio
async def test_agent_sees_view_and_edits_show_up(env):
    c, doc_id, work = env
    from mcp.client.client import Client

    mcp_server.configure(work, work.parent / "out")
    # The UI reports what it shows...
    c.put("/api/session/view", json={"doc_id": doc_id, "page": 2, "label": "38", "selection": "weird table", "dirty": False})
    async with Client(mcp_server.server) as agent:
        r = await agent.call_tool("get_current_view", {})
        kinds = [x.type for x in r.content]
        assert "image" in kinds
        info = json.loads(next(x.text for x in r.content if x.type == "text"))
        assert info["page"] == 2 and info["selection"] == "weird table" and info["user_has_unsaved_edits"] is False
        # ...the agent fixes the page; the UI sees a new version stamped "claude".
        r = await agent.call_tool("set_page", {"doc_id": doc_id, "page": 2, "html": "<b>fixed</b><div>by claude</div>",
                                               "label": "38"})
        assert r.structured_content["version"] == 1
        page = c.get(f"/api/documents/{doc_id}/pages/2").json()
        assert page["changed_by"] == "claude" and page["html"] == "<p><strong>fixed</strong></p><p>by claude</p>"
        # ...and can ask the UI to navigate.
        await agent.call_tool("show_page", {"doc_id": doc_id, "page": 3, "note": "check the footnote"})
        s = c.get("/api/session").json()
        assert s["requested"]["page"] == 3 and s["requested"]["note"] == "check the footnote"
        c.delete("/api/session/requested")
        assert c.get("/api/session").json()["requested"] is None
        # no UI open -> friendly message instead of an error
        Session(work).path.unlink()
        r = await agent.call_tool("get_current_view", {})
        assert "No page is open" in r.content[0].text


@pytest.mark.anyio
async def test_agent_sets_properties(env):
    c, doc_id, work = env
    from mcp.client.client import Client

    mcp_server.configure(work, work.parent / "out")
    async with Client(mcp_server.server) as agent:
        r = await agent.call_tool("set_properties", {"doc_id": doc_id, "author": "B. Author", "language": "de"})
        assert r.structured_content["title"] == "Collab"  # omitted fields are kept
        d = c.get(f"/api/documents/{doc_id}").json()
        assert (d["title"], d["author"], d["language"]) == ("Collab", "B. Author", "de")
        r = await agent.call_tool("set_properties", {"doc_id": doc_id, "language": "not a code"})
        assert r.is_error and "language code" in r.content[0].text
    assert c.get(f"/api/documents/{doc_id}").json()["language"] == "de"


@pytest.mark.anyio
async def test_agent_sees_why_a_tool_failed(env):
    c, doc_id, work = env
    from mcp.client.client import Client

    mcp_server.configure(work, work.parent / "out")
    async with Client(mcp_server.server) as agent:
        async def error(tool, args):
            r = await agent.call_tool(tool, args)
            assert r.is_error
            return r.content[0].text

        assert "unknown document 'nope'" in await error("get_status", {"doc_id": "nope"})
        assert "list_documents" in await error("get_page", {"doc_id": "nope", "page": 1})
        for tool, args in [("get_page", {}), ("show_page", {}), ("set_page", {"html": "<p>x</p>"})]:
            assert "page 9 out of range 1..3" in await error(tool, {"doc_id": doc_id, "page": 9, **args})
        assert "not a PDF file" in await error("open_document", {"pdf_path": str(work / "missing.pdf")})
        assert "unknown backend" in await error("transcribe_pages", {"doc_id": doc_id, "backend": "nope"})
        assert "no build yet" in await error("get_output_html", {"doc_id": doc_id})
    # nothing was stored by the failed set_page
    assert c.get(f"/api/documents/{doc_id}").json()["pages"][0]["version"] == 0


def test_instructions_reach_the_prompt(env):
    c, doc_id, work = env
    from unscanner.document import Document

    doc = Document.load(work / doc_id)
    prompt = page_prompt(doc, 1, "write every equation as MathML")
    assert "ADDITIONAL INSTRUCTIONS" in prompt and "MathML" in prompt
    assert "ADDITIONAL INSTRUCTIONS" not in page_prompt(doc, 1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.anyio
async def test_mcp_mounted_in_web_app(tmp_path):
    import uvicorn
    from mcp.client.client import Client

    app = webapp.create_app(tmp_path / "work", tmp_path / "out", mount_mcp=True)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp") as agent:
            names = {t.name for t in (await agent.list_tools()).tools}
            assert {"get_current_view", "show_page", "set_page", "transcribe_pages"} <= names
            r = await agent.call_tool("list_documents", {})
            assert r.structured_content == {"result": []}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
