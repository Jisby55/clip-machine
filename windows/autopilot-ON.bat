@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autopilot-setup.ps1"
echo Runs hidden every 15 min (catches up after sleep). Log: %~dp0..\bot.log
echo Running the first cycle now (can take a few minutes)...
call "%~dp0run-autopilot.bat"
echo.
echo ===== last lines of bot.log =====
powershell -NoProfile -Command "Get-Content -Encoding UTF8 -Tail 25 '%~dp0..\bot.log'"
pause
