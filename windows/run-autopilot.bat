@echo off
rem One machine tick (LIVE): Threads autopilot + video factory + Shorts/TikTok posting. Output -> bot.log
cd /d "%~dp0.."
set DRY_RUN=0
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if "%BOT_MODEL%"=="" set BOT_MODEL=qwen3:8b
python -c "import yt_dlp, faster_whisper, googleapiclient, psutil, imageio_ffmpeg, PIL" 2>nul || python -m pip install -q requests youtube-transcript-api yt-dlp faster-whisper google-api-python-client google-auth-oauthlib psutil imageio-ffmpeg numpy pillow >> bot.log 2>&1
python machine.py tick >> bot.log 2>&1
