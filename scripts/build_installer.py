"""Build the Windows installer: dist/Unscanner-Setup-<version>.exe.

    python scripts/build_installer.py [--no-epubcheck]

It installs for the current user without administrator rights, puts Unscanner in the Start menu, and
opens the UI in its own window with no console (Unscanner.exe); unscanner-cli.exe is the command line.
Documents are kept in Documents\\Unscanner (src/unscanner/app.py). The prompts a library changes live
there too (Settings > Edit prompts), so a frozen build loses nothing a library needs to adapt; code
changes need a rebuild.

Steps: a build environment in build/installer/venv with this project, its window and keyring extras and
PyInstaller (a clean one, so nothing else installed on this machine ends up in the app); PyInstaller
with scripts/installer/unscanner.spec; the C++ runtime and epubcheck copied in; Inno Setup's compiler
with scripts/installer/unscanner.iss. Needs 64-bit Windows, network access on the first run, and Inno
Setup 6 (winget install JRSoftware.InnoSetup, or set ISCC to its ISCC.exe).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = ROOT / "build" / "installer"
INSTALLER = HERE / "installer"

sys.path.insert(0, str(HERE))
from build_portable import add_vc_runtime, project_info  # noqa: E402 - the same pieces as the zip

# favicon.svg's shapes on its 32-unit grid, drawn again here because Pillow cannot read SVG.
ICON_SHAPES = [
    ("rounded", (0, 0, 32, 32), 6, "#1d4ed8"),
    ("polygon", [(8, 4), (19, 4), (24, 9), (24, 28), (8, 28)], 0, "#ffffff"),
    ("polygon", [(19, 4), (19, 9), (24, 9)], 0, "#93b4f5"),
    ("rect", (8, 18, 24, 28), 0, "#c7d2e4"),
    ("rect", (11, 9, 17, 11.5), 0, "#1d4ed8"),
    ("rect", (11, 13.5, 21, 15), 0, "#1a1a1a"),
    ("rounded", (5, 17, 27, 19), 1, "#fbbf24"),
    ("rect", (11, 21, 21, 22.5), 0, "#8a96a8"),
    ("rect", (11, 24, 19, 25.5), 0, "#8a96a8"),
]
ICON_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def draw_icon(path: Path) -> Path:
    """The app icon as a Windows .ico, from ICON_SHAPES (drawn large, then scaled down smoothly)."""
    from PIL import Image, ImageDraw

    scale = 32  # 1024 px
    img = Image.new("RGBA", (32 * scale, 32 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for kind, geom, radius, fill in ICON_SHAPES:
        if kind == "polygon":
            draw.polygon([(x * scale, y * scale) for x, y in geom], fill=fill)
        else:
            box = [v * scale for v in geom]
            if kind == "rounded":
                draw.rounded_rectangle(box, radius=radius * scale, fill=fill)
            else:
                draw.rectangle(box, fill=fill)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(path, sizes=[(s, s) for s in ICON_SIZES])
    return path


def find_iscc() -> Path:
    candidates = [os.environ.get("ISCC", ""), shutil.which("iscc") or ""]
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        if base:
            candidates += [str(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"),
                           str(Path(base) / "Inno Setup 6" / "ISCC.exe")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise SystemExit("Inno Setup 6 not found: winget install JRSoftware.InnoSetup (or set ISCC to ISCC.exe)")


def build_env() -> Path:
    """build/installer/venv with this project (reinstalled every time, so it is the current source)."""
    env_dir = BUILD / "venv"
    python = env_dir / "Scripts" / "python.exe"
    if not python.exists():
        venv.create(env_dir, with_pip=True)
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    subprocess.run([*pip, f"{ROOT}[window,keyring]", "pyinstaller"], check=True)
    subprocess.run([*pip, "--force-reinstall", "--no-deps", str(ROOT)], check=True)
    return python


def build(epubcheck: bool = True) -> Path:
    version, _ = project_info(ROOT)
    icon = draw_icon(BUILD / "unscanner.ico")
    python = build_env()
    subprocess.run([str(python), "-m", "PyInstaller", str(INSTALLER / "unscanner.spec"), "--noconfirm",
                    "--distpath", str(BUILD / "dist"), "--workpath", str(BUILD / "work")],
                   check=True, env={**os.environ, "UNSCANNER_ICON": str(icon)})
    app = BUILD / "dist" / "Unscanner"
    add_vc_runtime(app / "_internal")  # onnxruntime (OCR) needs the C++ runtime; see build_portable.py
    checks = sorted((ROOT / "tools").glob("epubcheck*/epubcheck.jar"))
    if epubcheck and checks:
        shutil.copytree(checks[-1].parent, app / "tools" / checks[-1].parent.name, dirs_exist_ok=True)
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    subprocess.run([str(find_iscc()), "/Q", f"/DAppVersion={version}", f"/DSourceDir={app}", f"/DOutputDir={out}",
                    f"/DIconFile={icon}", str(INSTALLER / "unscanner.iss")], check=True)
    return out / f"Unscanner-Setup-{version}.exe"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-epubcheck", action="store_true", help="leave tools/epubcheck-* out (about 35 MB)")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("build the Windows installer on Windows")
    setup = build(epubcheck=not args.no_epubcheck)
    size = sum(p.stat().st_size for p in (BUILD / "dist" / "Unscanner").rglob("*") if p.is_file())
    print(f"{BUILD / 'dist' / 'Unscanner'}: {size / 1e6:.0f} MB installed")
    print(f"{setup}: {setup.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
