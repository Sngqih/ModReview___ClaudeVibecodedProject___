@echo off
title ModReview
rem Double-click to run ModReview. Close this window to stop it.
cd /d "%~dp0"

where uv >nul 2>nul || (
  echo uv is not installed. Run "pip install uv" and try again.
  pause
  exit /b 1
)

rem Keeps the Python environment in step with this folder, even after a move.
uv sync --quiet || (pause & exit /b 1)

echo Starting ModReview at http://127.0.0.1:8765 - close this window to stop it.
uv run modreview
pause
