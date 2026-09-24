"""The installed app (src/unscanner/app.py) and scripts/build_installer.py, offline: nothing is built."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from unscanner import app, desktop, validate

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_installer", ROOT / "scripts" / "build_installer.py")
bi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bi)


def test_documents_go_to_documents_unless_the_command_says_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "documents_dir", lambda: tmp_path / "Documents")
    data = tmp_path / "Documents" / "Unscanner"
    assert app.with_data_dirs(["ui"]) == ["--work", str(data / "work"), "--out", str(data / "out"), "ui"]
    assert app.with_data_dirs(["--work", "w", "status", "x"]) == ["--out", str(data / "out"), "--work", "w", "status", "x"]
    assert app.with_data_dirs(["--work=w", "--out=o", "ui"]) == ["--work=w", "--out=o", "ui"]


def test_documents_dir_is_a_real_folder():
    assert app.documents_dir().is_dir()


def test_the_programs_run_the_cli_with_those_folders(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(app, "documents_dir", lambda: tmp_path)
    monkeypatch.setattr("unscanner.cli.main", lambda argv=None: seen.append(list(sys.argv[1:])))
    monkeypatch.setattr(desktop, "webview_storage", None)
    monkeypatch.setattr(sys, "argv", ["Unscanner.exe"])
    app.gui()
    monkeypatch.setattr(sys, "argv", ["unscanner-cli.exe", "status", "x.pdf"])
    app.console()
    work, out = str(tmp_path / "Unscanner" / "work"), str(tmp_path / "Unscanner" / "out")
    assert seen == [["--work", work, "--out", out, "ui"], ["--work", work, "--out", out, "status", "x.pdf"]]
    # the window's browser data stays out of Documents (and out of OneDrive)
    assert desktop.webview_storage.parts[-2:] == ("Unscanner", "webview")
    assert "Documents" not in desktop.webview_storage.parts


def test_the_installed_app_reopens_with_its_own_programs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Unscanner.exe"))
    assert desktop._command(["ui", "--browser"], console=True) == [str(tmp_path / "unscanner-cli.exe"), "ui", "--browser"]
    assert desktop._command(["ui"], console=False) == [str(tmp_path / "Unscanner.exe"), "ui"]
    assert desktop.pythonw() is None  # Unscanner.exe has no console already: nothing to hand over to


def test_epubcheck_is_found_next_to_the_installed_program(tmp_path, monkeypatch):
    jar = tmp_path / "tools" / "epubcheck-5.3.0" / "epubcheck.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"")
    monkeypatch.delenv("EPUBCHECK_JAR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Unscanner.exe"))
    assert validate.find_epubcheck() == str(jar)


def test_the_icon_has_every_size_windows_asks_for(tmp_path):
    from PIL import Image

    ico = bi.draw_icon(tmp_path / "unscanner.ico")
    with Image.open(ico) as img:
        assert {(s, s) for s in bi.ICON_SIZES} <= img.info["sizes"]
        img.size = (256, 256)
        img.load()
        assert img.getpixel((100, 64))[:3] == (255, 255, 255)  # the page
        assert img.getpixel((4, 128))[:3] == (0x1d, 0x4e, 0xd8)  # the blue tile


def test_iscc_is_found_from_the_environment(tmp_path, monkeypatch):
    fake = tmp_path / "ISCC.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("ISCC", str(fake))
    assert bi.find_iscc() == fake
    monkeypatch.setenv("ISCC", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(bi.shutil, "which", lambda name: None)
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(tmp_path / "nowhere"))
    with pytest.raises(SystemExit, match="winget install JRSoftware.InnoSetup"):
        bi.find_iscc()


def test_the_installer_is_per_user_and_keeps_its_app_id():
    iss = (ROOT / "scripts" / "installer" / "unscanner.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss  # no administrator rights, no admin prompt
    assert "DefaultDirName={autopf}\\Unscanner" in iss  # {autopf}: AppData\Local\Programs when per-user
    assert "AppId={{717F4491-2194-4F78-9C6C-FBAB87F4C7B4}" in iss  # upgrades find the installed copy by it
    spec = (ROOT / "scripts" / "installer" / "unscanner.spec").read_text(encoding="utf-8")
    assert '"Unscanner", console=False' in spec and '"unscanner-cli", console=True' in spec
    assert (app.GUI_EXE, app.CLI_EXE) == ("Unscanner.exe", "unscanner-cli.exe")
