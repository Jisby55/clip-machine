@echo off
rem Every 5 minutes: answer everyone who commented or replied (Threads, Instagram, YouTube). Output -> bot.log
cd /d "%~dp0.."
set DRY_RUN=0
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if "%BOT_MODEL%"=="" set BOT_MODEL=qwen3:8b
python machine.py replies >> bot.log 2>&1
