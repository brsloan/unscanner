@echo off
rem Double-click to start the unscanner web UI (http://127.0.0.1:8765).
rem Close this window, or press Ctrl+C in it, to stop the app.
rem The portable copy (scripts\build_portable.py) has its own Python in python\; otherwise use the one on PATH.
rem -s keeps the portable Python away from packages installed for the user's own Python 3.x.
title unscanner
cd /d "%~dp0"
set "PY=python"
set "PYFLAGS="
if exist "%~dp0python\python.exe" (
    set "PY=%~dp0python\python.exe"
    set "PYFLAGS=-s"
)
"%PY%" %PYFLAGS% -m unscanner.cli ui %*
if errorlevel 1 (
    echo.
    echo unscanner stopped with an error, see above.
    if not exist "%~dp0python\python.exe" (
        echo If Python or the package is missing, run:
        echo     pip install -e .[dev]
    )
    pause
)
