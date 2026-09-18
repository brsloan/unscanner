"""Shared "what is the user looking at" state between the web UI and the MCP server.

Kept in a small JSON file under the work directory so it works whether the MCP server runs inside
the web app process or as a separate stdio process started by Claude Desktop / Claude Code.

  view      what the UI currently shows: doc_id, page, label, selected text, when
  requested a page an agent asked the UI to navigate to (show_page); the UI follows it once
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

FILE = "session.json"


class Session:
    def __init__(self, work_root: str | Path):
        self.path = Path(work_root) / FILE

    def get(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"view": None, "requested": None}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"view": None, "requested": None}
        data.setdefault("view", None)
        data.setdefault("requested", None)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def update_view(self, doc_id: str, page: int, label: str | None = None, selection: str = "",
                    dirty: bool = False) -> dict[str, Any]:
        data = self.get()
        data["view"] = {"doc_id": doc_id, "page": page, "label": label, "selection": (selection or "")[:2000],
                        "dirty": dirty, "at": time.time()}
        self._write(data)
        return data

    def request_view(self, doc_id: str, page: int, note: str = "") -> dict[str, Any]:
        data = self.get()
        data["requested"] = {"doc_id": doc_id, "page": page, "note": note, "at": time.time()}
        self._write(data)
        return data

    def clear_request(self) -> None:
        data = self.get()
        if data.get("requested"):
            data["requested"] = None
            self._write(data)
