@echo off
cd /d "%~dp0.."
python -c "import tkinter" 2>nul || (echo Your Python is missing Tkinter. Reinstall Python from python.org with tcl/tk ticked. & pause & exit /b)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make-desktop-shortcut.ps1"
start "" wscript.exe "%~dp0Clip Machine.vbs"
timeout /t 3 >nul
