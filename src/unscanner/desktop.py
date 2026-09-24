"""The web UI as a desktop app: its own window (pywebview) and, on Windows, no console window.

The installed app's Unscanner.exe (app.py) is built with no console, so it starts out as the copy
described next, and hands over to unscanner-cli.exe when it needs a console.

start-unscanner.bat runs `unscanner ui --no-console`. That process checks, while it still has a console
to report to, that a window can be shown and the port is free; then it starts the same command again
with pythonw.exe (no console) and exits, which closes the console. The pythonw copy writes its output
to work/unscanner.log, shows an error that stops it in a message box, and closing its window stops it.
When no window can be shown the app stays in the console and uses the browser, because a browser tab
cannot stop the app and the console can (Ctrl+C, or closing it).
"""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

LOG_NAME = "unscanner.log"
LOG_MAX_BYTES = 1_000_000  # then it becomes unscanner.log.1 and a new one starts
PAUSE_ENV = "UNSCANNER_PAUSE_ON_ERROR"  # a console opened by reopen_with_console waits before closing on an error
log_path: Path | None = None  # set by log_to_file: this copy has no console, errors go to a message box
webview_storage: Path | None = None  # the window's browser data; None: work/.webview (app.gui sets AppData)
window: Any = None  # the pywebview window while it is open, for pick_file
FILE_TYPES = {"pdf": ("PDF files (*.pdf)",), "project": ("Unscanner project files (*.unscanner.json)", "JSON files (*.json)")}

# The WebView2 runtime's registry key (per machine, and per user); pywebview checks the same ones.
WEBVIEW2_KEYS = (("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
                 ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\EdgeUpdate\Clients"))
WEBVIEW2_RUNTIME = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


# ---------------------------------------------------------------- the window

def webview2_installed() -> bool:
    import winreg

    for hive, path in WEBVIEW2_KEYS:
        try:
            with winreg.OpenKey(getattr(winreg, hive), rf"{path}\{WEBVIEW2_RUNTIME}") as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if str(version).strip("0.") != "":  # an uninstalled runtime can leave "0.0.0.0" behind
            return True
    return False


def window_engine() -> tuple[Any, str]:
    """(the pywebview module, "") when the UI can open in its own window, else (None, why not).

    Without the WebView2 runtime pywebview falls back to Internet Explorer's engine, which cannot run
    this page, so that counts as no window."""
    try:
        import webview
    except ImportError:
        return None, ""  # not installed: the browser, as always, nothing to explain
    if sys.platform == "win32" and not webview2_installed():
        return None, "the Microsoft Edge WebView2 Runtime is not installed"
    return webview, ""


def open_window(webview: Any, url: str, work_root: str | Path) -> None:
    """Show the UI in a window and return when the person closes it."""
    webview.settings["ALLOW_DOWNLOADS"] = True  # Export project, HTML, EPUB: a Save dialog, as in a browser
    # The built HTML preview is a target=_blank link; OPEN_EXTERNAL_LINKS_IN_BROWSER (on by default) sends
    # it to the browser. text_select and zoomable are off by default in pywebview; people need both.
    # Maximized: the scan and the editor side by side need the room; width and height are the size it
    # restores to.
    global window
    window = webview.create_window("Unscanner", url, width=1400, height=900, min_size=(800, 500), maximized=True,
                                   text_select=True, zoomable=True)
    # Not private: the page keeps unsaved drafts, layout and choices in localStorage between runs. They
    # live in the work folder, so they move with it (the installed app keeps them in AppData instead).
    storage = webview_storage or Path(work_root).resolve() / ".webview"
    try:
        webview.start(private_mode=False, storage_path=str(storage))
    finally:
        window = None


def pick_file(kind: str, directory: str = "") -> str | None:
    """The system's Open dialog, for a file of `kind` (FILE_TYPES): the path chosen, None when cancelled.
    Raises RuntimeError when the UI is not in a window (a browser tab picks files its own way, and the
    file is uploaded). Called from the server thread; pywebview hands the dialog to the window's thread."""
    if window is None:
        raise RuntimeError("no window")
    import webview

    dialog = getattr(webview, "FileDialog", None)
    open_dialog = dialog.OPEN if dialog is not None else webview.OPEN_DIALOG
    chosen = window.create_file_dialog(open_dialog, directory=directory, allow_multiple=False,
                                       file_types=FILE_TYPES[kind])
    return str(chosen[0]) if chosen else None


def set_title(title: str) -> None:
    """Put `title` in the window's title bar (the page keeps its own document.title for a browser tab).
    Raises RuntimeError when the UI is not in a window."""
    if window is None:
        raise RuntimeError("no window")
    window.set_title(title)


# ---------------------------------------------------------------- without a console

def has_console() -> bool:
    """False under pythonw.exe, which starts with no stdout or stderr at all."""
    return sys.stdout is not None and sys.stderr is not None


def frozen() -> bool:
    """The installed app (PyInstaller): Unscanner.exe has no console, unscanner-cli.exe has one."""
    return bool(getattr(sys, "frozen", False))


def pythonw() -> Path | None:
    """The console-less twin of the running Python, when there is one (Windows only)."""
    if sys.platform != "win32" or frozen():
        return None
    exe = Path(sys.executable).with_name("pythonw.exe")
    return exe if exe.exists() else None


def port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _command(argv: list[str], console: bool) -> list[str]:
    """`unscanner <argv>` as a command line, run with (console=True) or without a console."""
    if frozen():
        from .app import CLI_EXE, GUI_EXE

        return [str(Path(sys.executable).with_name(CLI_EXE if console else GUI_EXE)), *argv]
    python = Path(sys.executable).with_name("python.exe") if console else pythonw()
    flags = ["-s"] if sys.flags.no_user_site else []  # the portable copy runs with -s; keep it
    return [str(python), *flags, "-m", "unscanner.cli", *argv]


def detach(argv: list[str]) -> None:
    """Start `unscanner <argv>` again with pythonw.exe, not tied to this console, and return at once."""
    subprocess.Popen(_command(argv, console=False), close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def reopen_with_console(argv: list[str] | None = None) -> None:
    """From a pythonw copy that cannot show its window: start `unscanner <argv> --browser` (argv: this
    copy's own) in a console window of its own, where the app can be stopped, and which stays open to
    show an error."""
    argv = sys.argv[1:] if argv is None else argv
    subprocess.Popen(_command([*argv, "--browser"], console=True), creationflags=subprocess.CREATE_NEW_CONSOLE,
                     env={**os.environ, PAUSE_ENV: "1"})


def log_to_file(work_root: str | Path) -> Path:
    """Send stdout and stderr (prints, uvicorn's and pywebview's logs, tracebacks) to work/unscanner.log."""
    global log_path
    path = Path(work_root) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(LOG_NAME + ".1"))
    log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 - lives as long as the app
    sys.stdout = sys.stderr = log
    print(f"==== {dt.datetime.now():%Y-%m-%d %H:%M:%S} unscanner {' '.join(sys.argv[1:])}")
    log_path = path.resolve()
    return path


def show_error(message: str) -> None:
    """An error the person has to see: into the log, and in a message box when there is no console."""
    print(message, file=sys.stderr)
    if log_path is not None and sys.platform == "win32":
        text = f"{message}\n\nDetails are in {log_path}."
        ctypes.windll.user32.MessageBoxW(None, text, "Unscanner", 0x10)  # 0x10: the error icon
