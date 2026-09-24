"""Entry points of the installed app (scripts/build_installer.py builds it with PyInstaller and Inno Setup).

    Unscanner.exe       the web UI in its own window, with no console (the Start menu shortcut)
    unscanner-cli.exe   the command line, and the MCP server for Claude (`unscanner-cli.exe serve`)

(Windows file names ignore case, so the command line cannot be called unscanner.exe next to Unscanner.exe.)
Both keep documents and output in Documents\\Unscanner, not in the program folder, so an upgrade or an
uninstall never touches them; --work / --out still choose another place. The window's browser data goes
to AppData\\Local\\Unscanner, which OneDrive does not sync.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

GUI_EXE = "Unscanner.exe"
CLI_EXE = "unscanner-cli.exe"


def documents_dir() -> Path:
    """The user's Documents folder, wherever Windows has put it (OneDrive, a network share)."""
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # 5: CSIDL_PERSONAL
            return Path(buf.value)
    return Path.home() / "Documents"


def data_dir() -> Path:
    return documents_dir() / "Unscanner"


def with_data_dirs(argv: list[str]) -> list[str]:
    """argv with --work and --out in Documents\\Unscanner unless it names them itself."""
    extra = []
    for flag, sub in (("--work", "work"), ("--out", "out")):
        if not any(a == flag or a.startswith(flag + "=") for a in argv):
            extra += [flag, str(data_dir() / sub)]
    return extra + argv


def _run(argv: list[str]) -> None:
    from . import cli

    # Everything downstream (the command a copy reopens itself with, the log header) reads sys.argv.
    sys.argv = [sys.argv[0], *argv]
    cli.main()


def gui() -> None:
    """Unscanner.exe: `unscanner ui`, no console."""
    from . import desktop

    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    desktop.webview_storage = local / "Unscanner" / "webview"
    _run(with_data_dirs(["ui", *sys.argv[1:]]))


def console() -> None:
    """unscanner-cli.exe: the whole command line."""
    _run(with_data_dirs(sys.argv[1:]))
