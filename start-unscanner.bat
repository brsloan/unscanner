@echo off
rem Double-click to start Unscanner (http://127.0.0.1:8765). When pywebview is installed (pip install -e .[window];
rem the portable copy has it) it opens in its own window and this console closes after a moment: close the
rem window to stop the app, and look in work\unscanner.log for its messages. Otherwise it opens in the browser
rem and this console stays: close it, or press Ctrl+C in it, to stop the app.
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
"%PY%" %PYFLAGS% -m unscanner.cli ui --no-console %*
if errorlevel 1 (
    echo.
    echo unscanner stopped with an error, see above.
    if not exist "%~dp0python\python.exe" (
        echo If Python or the package is missing, run:
        echo     pip install -e .[dev]
    )
    pause
)
