@echo off
rem Double-click to start the remediate web UI (http://127.0.0.1:8765).
rem Close this window, or press Ctrl+C in it, to stop the app.
title remediate
cd /d "%~dp0"
python -m remediate.cli ui %*
if errorlevel 1 (
    echo.
    echo remediate stopped with an error. If Python or the package is missing, run:
    echo     pip install -e .[dev]
    pause
)
