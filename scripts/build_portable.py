"""Build a portable Windows copy of unscanner: unzip it anywhere and double-click start-unscanner.bat.

    python scripts/build_portable.py [--python 3.13.7] [--python-zip FILE] [--no-epubcheck]

The folder it makes (dist/unscanner/, zipped as dist/unscanner-<version>-win64.zip) holds

    start-unscanner.bat   the web UI (the same launcher as the repo; it prefers python\\python.exe)
    unscanner.bat         the command line, with work\\ and out\\ kept in this folder
    python\\               Windows' embeddable Python from python.org, dependencies in Lib\\site-packages
    src\\unscanner\\        the program as plain .py files, so a library can still adapt it in place
    tools\\epubcheck-*\\    copied when the repo has it (it still needs Java on the machine)

Run it on 64-bit Windows with the same Python minor version as the embedded one (pip installs wheels
for the Python that runs it). It downloads the embeddable zip from python.org unless --python-zip is
given, and the dependencies in pyproject.toml from PyPI.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMBED_URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"

# onnxruntime (RapidOCR, used for pages with no text layer) links against the C++ runtime, which the
# embeddable Python does not ship; a machine without the Visual C++ Redistributable would fail to
# import it. Microsoft allows these files to be copied next to the program that uses them.
VC_RUNTIME = ("msvcp140.dll", "msvcp140_1.dll")

# Parts of dependencies unscanner never uses, left out to keep the zip small.
TRIM = ("bin",                                  # pip's .exe wrappers, tied to the Python that built them
        "cv2/opencv_videoio_ffmpeg*.dll")       # OpenCV's video reader (~30 MB); RapidOCR reads images

PORTABLE_README = """\
unscanner, portable copy for Windows
====================================

Start: double-click start-unscanner.bat. The web UI opens in your browser at http://127.0.0.1:8765.
Close the black window to stop it. Nothing is installed and no admin rights are needed; your work is
kept in the work\\ and out\\ folders next to this file.

If Windows says it protected your PC: right-click the downloaded zip, Properties, tick Unblock, OK,
and unzip it again (or click More info, Run anyway).

Before the first real run, open Settings > Edit prompts and make "Who is doing the work and why" true
for your institution (see README.md). Your prompts are kept in work\\prompts\\.
Everything in src\\unscanner is plain Python and takes effect the next time you start the app.

Command line: unscanner.bat <command> ..., e.g.  unscanner.bat status work\\my-reading

Claude Desktop / Claude Code: while the UI is open, add the MCP server http://127.0.0.1:8765/mcp.
Without the UI, run it over stdio with this folder's Python:
    command: <this folder>\\python\\python.exe
    args:    -s -m unscanner.cli --work <this folder>\\work --out <this folder>\\out serve
(-s keeps it away from Python packages installed elsewhere on the machine.)

EPUB validation (epubcheck) runs only when Java 21 or newer is installed.

Updating: unzip the new version to a new folder and move your work\\ folder across. It holds your
documents, settings and prompts.
"""

CLI_BAT = """\
@echo off
rem unscanner command line, e.g.  unscanner.bat status work\\my-reading
rem Documents are kept in this folder's work\\ and out\\ wherever you run it from.
"%~dp0python\\python.exe" -s -m unscanner.cli --work "%~dp0work" --out "%~dp0out" %*
"""


def project_info(root: Path = ROOT) -> tuple[str, list[str]]:
    """The version and the runtime dependencies (plus the keyring extra) from pyproject.toml."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["version"], project["dependencies"] + project["optional-dependencies"]["keyring"]


def fetch_python(version: str, cache: Path) -> Path:
    dest = cache / f"python-{version}-embed-amd64.zip"
    if not dest.exists():
        cache.mkdir(parents=True, exist_ok=True)
        url = EMBED_URL.format(v=version)
        print(f"downloading {url}", flush=True)
        with urllib.request.urlopen(url) as r, open(dest.with_suffix(".part"), "wb") as f:
            shutil.copyfileobj(r, f)
        dest.with_suffix(".part").rename(dest)
    return dest


def unpack_python(embed_zip: Path, python_dir: Path) -> None:
    """Unzip the embeddable Python and point its ._pth file at ..\\src and at Lib\\site-packages."""
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(python_dir)
    pth = next(python_dir.glob("python*._pth"))
    # The ._pth file replaces the search path (PYTHONPATH and the registry are ignored) and "import site"
    # adds Lib\site-packages. site would still add the user's own site-packages for this Python version
    # (%APPDATA%\Python\Python313), so the launchers run python.exe -s.
    lines = [ln for ln in pth.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    lines = [ln for ln in lines if ln.strip() != "import site"]
    pth.write_text("\n".join(lines + ["..\\src", "import site"]) + "\n", encoding="utf-8")


def install_dependencies(deps: list[str], site_packages: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(site_packages), "--only-binary=:all:",
                    "--no-compile", "--disable-pip-version-check", *deps], check=True)


def add_vc_runtime(python_dir: Path) -> None:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in VC_RUNTIME:
        if (system32 / name).exists() and not (python_dir / name).exists():
            shutil.copy2(system32 / name, python_dir / name)
        elif not (python_dir / name).exists():
            print(f"warning: {name} not found; OCR of pages without a text layer needs the Visual C++ Redistributable")


def trim(site_packages: Path) -> None:
    for pattern in TRIM:
        for p in site_packages.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    for p in list(site_packages.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True)


def copy_app(root: Path, dest: Path, epubcheck: bool = True) -> None:
    shutil.copytree(root / "src" / "unscanner", dest / "src" / "unscanner",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("README.md", "CLAUDE.md", "LICENSE"):
        shutil.copy2(root / name, dest / name)
    # cmd.exe expects CRLF; a checkout may have LF
    launcher = (root / "start-unscanner.bat").read_text(encoding="ascii").replace("\r\n", "\n")
    (dest / "start-unscanner.bat").write_text(launcher.replace("\n", "\r\n"), encoding="ascii", newline="")
    (dest / "unscanner.bat").write_text(CLI_BAT.replace("\n", "\r\n"), encoding="ascii", newline="")
    (dest / "PORTABLE.txt").write_text(PORTABLE_README.replace("\n", "\r\n"), encoding="utf-8", newline="")
    checks = sorted((root / "tools").glob("epubcheck*/epubcheck.jar"))
    if epubcheck and checks:
        shutil.copytree(checks[-1].parent, dest / "tools" / checks[-1].parent.name)


def make_zip(folder: Path, zip_path: Path) -> None:
    """Zip the folder with itself at the top, so unzipping gives one unscanner\\ folder."""
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(folder.parent))


def build(root: Path, out: Path, embed_zip: Path, install: bool = True, epubcheck: bool = True) -> Path:
    """Make out/unscanner/ and out/unscanner-<version>-win64.zip; return the zip's path.
    install=False skips pip and the C++ runtime (the tests build a skeleton that way, offline)."""
    version, deps = project_info(root)
    folder = out / "unscanner"
    if folder.exists():
        shutil.rmtree(folder)
    python_dir = folder / "python"
    unpack_python(embed_zip, python_dir)
    site_packages = python_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    if install:
        install_dependencies(deps, site_packages)
        add_vc_runtime(python_dir)
    trim(site_packages)
    copy_app(root, folder, epubcheck=epubcheck)
    zip_path = out / f"unscanner-{version}-win64.zip"
    make_zip(folder, zip_path)
    return zip_path


def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> None:
    here = ".".join(map(str, sys.version_info[:3]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=here, help=f"embeddable Python version (default: this one, {here})")
    ap.add_argument("--python-zip", type=Path, help="an embeddable Python zip already on disk (skips the download)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder (default: dist)")
    ap.add_argument("--no-epubcheck", action="store_true", help="leave tools/epubcheck-* out (about 35 MB)")
    args = ap.parse_args(argv)

    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("build this on 64-bit Windows: pip installs wheels for the machine it runs on")
    if args.python.split(".")[:2] != here.split(".")[:2]:
        raise SystemExit(f"run this script with Python {'.'.join(args.python.split('.')[:2])} to embed {args.python}")
    embed_zip = args.python_zip or fetch_python(args.python, ROOT / "build" / "portable")
    zip_path = build(ROOT, args.out, embed_zip, epubcheck=not args.no_epubcheck)
    print(f"{args.out / 'unscanner'}: {folder_size(args.out / 'unscanner') / 1e6:.0f} MB unpacked")
    print(f"{zip_path}: {zip_path.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
