@echo off
title ClipForge  (close this window to stop)
echo Starting ClipForge... your browser will open in a moment.
echo Keep this window open while you work. Close it (or press Ctrl+C) to stop.
echo.
set "PYTHONPATH=C:\Users\Brett\OneDrive\Documents\StreamingProject\clipper"
"C:\Users\Brett\clipforge\.venv\Scripts\python.exe" "%PYTHONPATH%\dashboard.py"
echo.
echo ClipForge has stopped. You can close this window.
pause >nul
