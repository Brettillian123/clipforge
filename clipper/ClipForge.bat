@echo off
title ClipForge  (close this window to stop)
echo Starting ClipForge...
echo Keep this window open while you work. Close it (or press Ctrl+C) to stop.
echo.
rem Run from this script's own folder, so it works wherever the repo is cloned.
set "PYTHONPATH=%~dp0"
rem Prefer the dedicated venv under your clipforge home (CLIPFORGE_HOME or %USERPROFILE%\clipforge);
rem fall back to whatever "python" is on PATH.
set "CF_HOME=%CLIPFORGE_HOME%"
if "%CF_HOME%"=="" set "CF_HOME=%USERPROFILE%\clipforge"
set "PY=%CF_HOME%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0dashboard.py"
echo.
echo ClipForge has stopped. You can close this window.
pause >nul
