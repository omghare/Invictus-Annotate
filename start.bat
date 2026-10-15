@echo off
rem Invictus Annotate — double-click to start the server (uses the venv in this folder).
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo No venv found. Set it up first — see INSTALL.md
  pause
  exit /b 1
)
"venv\Scripts\python.exe" run.py
pause
