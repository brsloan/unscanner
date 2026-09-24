"""`unscanner ui` in its own window (pywebview), and the browser when there is no window to be had.
A stand-in webview module keeps these offline and headless; the server they start is real."""

from __future__ import annotations

import socket
import sys
import types
import urllib.request

import pytest
import uvicorn

from unscanner import cli, desktop, webapp


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeWebview(types.SimpleNamespace):
    """What serve() uses of pywebview. start() plays the person: it loads the page, then closes the window."""

    def __init__(self, fail: bool = False):
        super().__init__(settings={"ALLOW_DOWNLOADS": False}, windows=[], started=[], pages=[], fail=fail)

    def create_window(self, title, url, **kw):
        self.windows.append((title, url, kw))

    def start(self, **kw):
        if self.fail:
            raise RuntimeError("no GUI here")
        self.started.append(kw)
        with urllib.request.urlopen(self.windows[-1][1], timeout=5) as r:
            self.pages.append(r.read().decode())


class RecordingServer(uvicorn.Server):
    last: "RecordingServer | None" = None

    def __init__(self, config):
        super().__init__(config)
        RecordingServer.last = self


def test_no_pywebview_means_the_browser_without_a_word(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview -> ImportError
    assert desktop.window_engine() == (None, "")


def test_no_webview2_means_the_browser_and_says_why(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", FakeWebview())
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop, "webview2_installed", lambda: False)
    engine, why = desktop.window_engine()
    assert engine is None and "WebView2" in why
    monkeypatch.setattr(desktop, "webview2_installed", lambda: True)
    assert desktop.window_engine() == (sys.modules["webview"], "")


def test_the_ui_opens_in_a_window_and_closing_it_stops_the_server(tmp_path, monkeypatch):
    fake = FakeWebview()
    monkeypatch.setattr(desktop, "window_engine", lambda: (fake, ""))
    monkeypatch.setattr(uvicorn, "Server", RecordingServer)
    port = free_port()
    webapp.serve(tmp_path / "work", tmp_path / "out", port=port)  # returns once the "window" is closed

    title, url, kw = fake.windows[0]
    assert title == "Unscanner" and url == f"http://127.0.0.1:{port}/"
    assert "<title>Unscanner</title>" in fake.pages[0]  # the server was up while the window was open
    assert kw["text_select"] and kw["zoomable"]  # both off by default in pywebview
    assert kw["maximized"]
    assert fake.settings["ALLOW_DOWNLOADS"] is True  # Export project, HTML and EPUB downloads
    # drafts and layout persist between runs, in the work folder
    assert fake.started[0] == {"private_mode": False, "storage_path": str((tmp_path / "work" / ".webview").resolve())}
    assert RecordingServer.last.should_exit
    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=2)


def test_a_window_that_fails_falls_back_to_the_browser(tmp_path, monkeypatch, capsys):
    fake = FakeWebview(fail=True)
    monkeypatch.setattr(desktop, "window_engine", lambda: (fake, ""))
    monkeypatch.setattr(uvicorn, "Server", RecordingServer)
    opened = []

    def browser(url):  # the person uses the page in the browser, then stops the app
        with urllib.request.urlopen(url, timeout=5) as r:
            opened.append((url, r.status))
        RecordingServer.last.should_exit = True

    monkeypatch.setattr("webbrowser.open", browser)
    port = free_port()
    webapp.serve(tmp_path / "work", tmp_path / "out", port=port)
    assert opened == [(f"http://127.0.0.1:{port}/", 200)]
    assert "Could not open a window (RuntimeError: no GUI here)" in capsys.readouterr().out


def test_cli_browser_flag_skips_the_window(monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "serve", lambda *a, **k: calls.append(k))
    cli.main(["ui", "--browser"])
    cli.main(["ui", "--no-browser"])
    cli.main(["ui"])
    assert [(k["window"], k["open_browser"]) for k in calls] == [(False, True), (True, False), (True, True)]


# ---------------------------------------------------------------- no console (start-unscanner.bat)

class Popen:
    calls: list = []

    def __init__(self, cmd, **kw):
        Popen.calls.append((cmd, kw))


@pytest.fixture
def popen(monkeypatch):
    Popen.calls = []
    monkeypatch.setattr(desktop.subprocess, "Popen", Popen)
    return Popen.calls


def test_no_console_hands_over_to_pythonw_when_a_window_can_open(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(desktop, "pythonw", lambda: tmp_path / "pythonw.exe")
    monkeypatch.setattr(desktop, "window_engine", lambda: (FakeWebview(), ""))
    monkeypatch.setattr(webapp, "serve", lambda *a, **k: pytest.fail("the console copy must not serve"))
    port = free_port()
    cli.main(["--work", "w", "ui", "--no-console", "--port", str(port)])
    (cmd, kw), = popen
    assert cmd[0] == str(tmp_path / "pythonw.exe") and cmd[-6:] == ["unscanner.cli", "--work", "w", "ui", "--port", str(port)]
    assert kw["creationflags"] & desktop.subprocess.DETACHED_PROCESS


def test_no_console_stays_in_the_console_without_a_window_or_with_the_port_taken(monkeypatch, popen, tmp_path):
    calls = []
    monkeypatch.setattr(webapp, "serve", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(desktop, "pythonw", lambda: tmp_path / "pythonw.exe")
    monkeypatch.setattr(desktop, "window_engine", lambda: (None, "the Microsoft Edge WebView2 Runtime is not installed"))
    cli.main(["ui", "--no-console"])  # no window: the browser, stopped from this console
    monkeypatch.setattr(desktop, "window_engine", lambda: (FakeWebview(), ""))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        cli.main(["ui", "--no-console", "--port", str(taken.getsockname()[1])])  # the error shows here
    assert popen == [] and [k["console"] for k in calls] == [True, True]


def test_the_pythonw_copy_logs_to_a_file_and_shows_errors_in_a_box(tmp_path, monkeypatch):
    boxes = []
    monkeypatch.setattr(desktop, "ctypes", types.SimpleNamespace(windll=types.SimpleNamespace(
        user32=types.SimpleNamespace(MessageBoxW=lambda _, text, title, flags: boxes.append((title, text))))))
    monkeypatch.setattr(desktop, "log_path", None)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", None)  # how pythonw.exe starts
    monkeypatch.setattr(sys, "stderr", None)
    seen = {}

    def serve(*a, **k):
        seen.update(k)
        print("a line from the app")
        raise RuntimeError("boom")

    monkeypatch.setattr(webapp, "serve", serve)
    with pytest.raises(SystemExit):
        cli.main(["--work", str(tmp_path / "work"), "ui"])
    sys.stdout.close()
    log = (tmp_path / "work" / "unscanner.log").read_text(encoding="utf-8")
    assert seen["console"] is False
    assert log.startswith("==== ") and "a line from the app" in log and "RuntimeError: boom" in log
    (title, text), = boxes
    assert title == "Unscanner" and "boom" in text and "unscanner.log" in text


def test_the_log_is_kept_small(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "log_path", None)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    (tmp_path / "unscanner.log").write_bytes(b"x" * (desktop.LOG_MAX_BYTES + 1))
    desktop.log_to_file(tmp_path)
    sys.stdout.close()
    assert (tmp_path / "unscanner.log.1").stat().st_size > desktop.LOG_MAX_BYTES
    assert (tmp_path / "unscanner.log").stat().st_size < 1000


def test_the_pythonw_copy_hands_over_to_a_console_when_its_window_fails(tmp_path, monkeypatch):
    reopened = []
    monkeypatch.setattr(desktop, "reopen_with_console", lambda: reopened.append(True))
    monkeypatch.setattr(uvicorn, "Server", RecordingServer)
    monkeypatch.setattr(desktop, "window_engine", lambda: (FakeWebview(fail=True), ""))
    webapp.serve(tmp_path / "work", tmp_path / "out", port=free_port(), console=False)
    assert reopened == [True] and RecordingServer.last.should_exit  # its own server stopped first
    monkeypatch.setattr(desktop, "window_engine", lambda: (None, "no WebView2"))
    webapp.serve(tmp_path / "work", tmp_path / "out", port=free_port(), console=False)
    assert reopened == [True, True]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # uvicorn exits its thread
def test_the_pythonw_copy_says_when_the_port_is_taken(tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(desktop, "show_error", errors.append)
    monkeypatch.setattr(desktop, "window_engine", lambda: (FakeWebview(), ""))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        webapp.serve(tmp_path / "work", tmp_path / "out", port=taken.getsockname()[1], console=False)
    (message,) = errors
    assert "could not start" in message and "already open" in message


def test_reopen_with_console_runs_the_browser_in_a_new_console(monkeypatch, popen):
    desktop.reopen_with_console(["--work", "w", "ui", "--port", "8765"])
    (cmd, kw), = popen
    assert cmd[0].endswith("python.exe") and cmd[-6:] == ["--work", "w", "ui", "--port", "8765", "--browser"]
    assert kw["creationflags"] == desktop.subprocess.CREATE_NEW_CONSOLE and kw["env"][desktop.PAUSE_ENV] == "1"
