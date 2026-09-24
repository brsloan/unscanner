"""scripts/build_portable.py, offline: a fake embeddable Python and no pip run."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_portable", ROOT / "scripts" / "build_portable.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

EMBED_PTH = "python313.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n"


def fake_embed_zip(tmp_path: Path) -> Path:
    p = tmp_path / "python-3.13.0-embed-amd64.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("python.exe", b"MZ")
        z.writestr("python313.zip", b"")
        z.writestr("python313._pth", EMBED_PTH)
    return p


def test_project_info_reads_dependencies_and_extras():
    version, deps = bp.project_info(ROOT)
    assert version
    names = " ".join(deps)
    assert "pymupdf" in names and "rapidocr" in names and "keyring" in names and "pywebview" in names


def test_version_is_the_same_in_pyproject_and_the_package():
    # The release workflow names the build after pyproject.toml and checks the tag against both.
    import unscanner

    assert bp.project_info(ROOT)[0] == unscanner.__version__


def test_build_lays_out_a_portable_folder_and_zip(tmp_path):
    zip_path = bp.build(ROOT, tmp_path / "dist", fake_embed_zip(tmp_path), install=False, epubcheck=False)
    folder = tmp_path / "dist" / "unscanner"

    # the search path is closed: stdlib zip, python\, the editable source, then site-packages via site
    pth = (folder / "python" / "python313._pth").read_text(encoding="utf-8").splitlines()
    assert pth == ["python313.zip", ".", "..\\src", "import site"]
    assert (folder / "python" / "Lib" / "site-packages").is_dir()

    assert (folder / "src" / "unscanner" / "prompts.py").is_file()
    assert (folder / "src" / "unscanner" / "web" / "index.html").is_file()
    assert not list((folder / "src").rglob("__pycache__"))
    assert not (folder / "tools").exists()

    launcher = (folder / "start-unscanner.bat").read_bytes()
    assert b"\r\n" in launcher and b"\n" not in launcher.replace(b"\r\n", b"")
    # -s: the user's own site-packages for this Python version must not leak into the portable copy
    assert b'set "PY=%~dp0python\\python.exe"' in launcher and b'set "PYFLAGS=-s"' in launcher
    cli = (folder / "unscanner.bat").read_bytes()
    assert b'python.exe" -s -m unscanner.cli --work "%~dp0work"' in cli
    assert (folder / "PORTABLE.txt").is_file()

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    assert all(n.startswith("unscanner/") for n in names)
    assert "unscanner/python/python.exe" in names and "unscanner/src/unscanner/prompts.py" in names


def test_build_replaces_an_earlier_build(tmp_path):
    embed = fake_embed_zip(tmp_path)
    bp.build(ROOT, tmp_path / "dist", embed, install=False, epubcheck=False)
    (tmp_path / "dist" / "unscanner" / "stale.txt").write_text("old")
    bp.build(ROOT, tmp_path / "dist", embed, install=False, epubcheck=False)
    assert not (tmp_path / "dist" / "unscanner" / "stale.txt").exists()


def test_trim_drops_video_codec_exe_wrappers_and_bytecode(tmp_path):
    sp = tmp_path / "site-packages"
    for rel in ("cv2/cv2.pyd", "cv2/opencv_videoio_ffmpeg500_64.dll", "bin/uvicorn.exe",
                "numpy/__pycache__/x.pyc", "numpy/__init__.py"):
        (sp / rel).parent.mkdir(parents=True, exist_ok=True)
        (sp / rel).write_bytes(b"x")
    bp.trim(sp)
    left = sorted(p.relative_to(sp).as_posix() for p in sp.rglob("*") if p.is_file())
    assert left == ["cv2/cv2.pyd", "numpy/__init__.py"]
