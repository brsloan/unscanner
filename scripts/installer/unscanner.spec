# PyInstaller spec for the installed app. scripts/build_installer.py runs it in its own build
# environment; see there. Two programs share one folder (and its _internal\ with Python and every
# dependency): Unscanner.exe, the UI with no console, and unscanner-cli.exe, the command line.
import os

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

HERE = SPECPATH  # noqa: F821 - PyInstaller defines it: this folder
ICON = os.environ["UNSCANNER_ICON"]  # drawn by build_installer.py

DATAS = (
    collect_data_files("unscanner")  # the web UI's page, script and styles
    + collect_data_files("rapidocr_onnxruntime")  # OCR models and their config
    + copy_metadata("unscanner")
)
EXCLUDES = ["tkinter", "pytest"]


def analysis(script):
    return Analysis([os.path.join(HERE, script)], datas=DATAS, excludes=EXCLUDES)  # noqa: F821


def program(a, name, console):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console,  # noqa: F821
               icon=ICON, upx=False)


gui, cli = analysis("gui.py"), analysis("cli.py")
COLLECT(program(gui, "Unscanner", console=False), gui.binaries, gui.datas,  # noqa: F821
        program(cli, "unscanner-cli", console=True), cli.binaries, cli.datas,
        name="Unscanner", upx=False)
