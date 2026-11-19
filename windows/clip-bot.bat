@echo off
title Clip Machine
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

where python >nul 2>nul || (echo Python is not installed or not on PATH. Install it from python.org and tick "Add to PATH". & pause & exit /b)
where ollama >nul 2>nul || (echo Ollama is not on PATH. Open the Ollama app first. & pause & exit /b)
python -c "import requests, youtube_transcript_api, yt_dlp, faster_whisper, googleapiclient, google_auth_oauthlib, psutil, imageio_ffmpeg, numpy, PIL" 2>nul || (echo Installing the machine's Python packages, one time... & python -m pip install -r requirements.txt)
where nvidia-smi >nul 2>nul && (python -c "import nvidia.cublas, nvidia.cudnn" 2>nul || (echo Installing NVIDIA libraries so clips render on the GPU - about 1 GB, one time... & python -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"))
if not exist profile.json (copy /y profile.example.json profile.json >nul & echo Created profile.json - fill in your name, channel and handles.)
if not exist machine_config.json copy /y machine_config.example.json machine_config.json >nul
if not exist secrets.env (copy /y secrets.env.example secrets.env >nul & echo Created secrets.env - paste your keys into it.)
where deno >nul 2>nul || (echo Installing Deno - the YouTube downloader needs it, one time... & winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements)
where cloudflared >nul 2>nul || if not exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (echo Installing cloudflared - Instagram picture posts need it, one time... & winget install -e --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements)
if "%BOT_MODEL%"=="" for /f "usebackq delims=" %%m in (`python -c "import json;print(json.load(open('machine_config.json')).get('ollama_model') or 'qwen3:8b')"`) do set BOT_MODEL=%%m
if "%BOT_MODEL%"=="" set BOT_MODEL=qwen3:8b
ollama show %BOT_MODEL% >nul 2>nul || (echo Downloading %BOT_MODEL% - about 6 GB, one time... & ollama pull %BOT_MODEL%)

if not defined DRY_RUN set DRY_RUN=1

:menu
cls
echo ===== CLIP MACHINE =====
schtasks /query /tn "ClipMachine Autopilot" >nul 2>nul && (echo Autopilot: ON - runs every 15 min by itself) || (echo Autopilot: OFF - Settings, option 14 turns it on)
echo.
echo   1. Status - what's ready, posted, and how it's doing
echo   2. Post NOW (pick a platform, skips the schedule)
echo   3. Make content NOW (clip, meme, carousel, AI image - nothing gets posted)
echo   4. Get MORE videos from my channel (downloads + finds funny moments)
echo   5. Run one full autopilot cycle here (live)
echo   6. Settings and accounts
echo   7. FULL AI CONTROL - the bot decides what to post and posts on ALL platforms now
echo   8. Chat with the bot
echo   0. Quit
echo.
set "c="
set /p "c=Pick a number: "
if "%c%"=="1" (python machine.py status & pause)
if "%c%"=="2" goto post
if "%c%"=="3" goto make
if "%c%"=="4" goto fetch
if "%c%"=="5" (set "DRY_RUN=0" & python machine.py tick & set "DRY_RUN=1" & pause)
if "%c%"=="6" goto settings
if "%c%"=="7" goto fullai
if "%c%"=="8" (python threadsbot.py chat & goto menu)
if "%c%"=="0" exit /b
goto menu

:post
cls
echo ===== POST NOW (real posts) =====
echo   1. Threads     2. YouTube Short     3. TikTok (to your inbox)     4. Instagram     5. ALL of them
echo   0. Back
set "p="
set /p "p=Pick: "
set "t="
if "%p%"=="1" set "t=threads"
if "%p%"=="2" set "t=youtube"
if "%p%"=="3" set "t=tiktok"
if "%p%"=="4" set "t=instagram"
if "%p%"=="5" set "t=all"
if not defined t goto menu
set "ok="
set /p "ok=This posts for real on %t%. Type Y to go: "
if /i not "%ok%"=="Y" goto menu
set "DRY_RUN=0"
python machine.py force %t%
set "DRY_RUN=1"
pause
goto menu

:fullai
cls
echo ===== FULL AI CONTROL =====
echo The bot picks what to post (one theme everywhere, or each platform does its own thing)
echo and posts on Threads, YouTube, TikTok (inbox) and Instagram right now. Takes a few minutes.
set "ok="
set /p "ok=Type Y to let it loose: "
if /i not "%ok%"=="Y" goto menu
set "DRY_RUN=0"
python machine.py full-ai
set "DRY_RUN=1"
pause
goto menu

:make
cls
echo ===== MAKE CONTENT NOW (saved, not posted) =====
echo   1. A clip (Short/TikTok)     2. A meme     3. A carousel     4. An AI image (ComfyUI must be open)
echo   0. Back
set "m="
set /p "m=Pick: "
if "%m%"=="1" (python machine.py produce & pause)
if "%m%"=="2" (python machine.py test-picture meme & pause)
if "%m%"=="3" (python machine.py test-picture carousel & pause)
if "%m%"=="4" (python machine.py test-picture ai_image & pause)
goto menu

:fetch
cls
echo ===== GET MORE VIDEOS FROM YOUR CHANNEL =====
echo The bot already works through your whole channel by itself (a couple of videos per run, streams and
echo less-viewed videos first, and it skips anything you already made a Short of). This just speeds it up.
echo Big videos take a while to download and analyze. Old used-up videos get deleted to save space.
set "n="
set /p "n=How many videos to pull in now (1-10, Enter = 3): "
if not defined n set "n=3"
python machine.py fetch %n%
pause
goto menu

:settings
cls
echo ===== SETTINGS AND ACCOUNTS =====
echo   1. GPU mode: auto (pause heavy work while gaming)     2. GPU mode: always on     3. GPU mode: off
echo   4. Light games list (games that DON'T pause it)
echo   5. Open the bot's brain files (memory, channel notes, keywords, settings)
echo   6. Connect YouTube Shorts channel    7. Connect TikTok    8. Connect Instagram
echo   9. Delete a Threads post             10. Check what TikTok did with uploads
echo  11. Threads self-check                12. Install full FFmpeg (only if captions are missing)
echo  13. Instagram: check stats + reply to comments now
echo  14. Fix / restart the autopilot schedule
echo   0. Back
set "s="
set /p "s=Pick: "
if "%s%"=="1" (python machine.py gpu auto & pause)
if "%s%"=="2" (python machine.py gpu on & pause)
if "%s%"=="3" (python machine.py gpu off & pause)
if "%s%"=="4" start notepad light_games.txt
if "%s%"=="5" (start notepad memory.md & start notepad channel.md & start notepad keywords.md & start notepad machine_config.json)
if "%s%"=="6" (python machine.py setup-youtube & pause)
if "%s%"=="7" (python machine.py setup-tiktok & pause)
if "%s%"=="8" (python machine.py setup-instagram & pause)
if "%s%"=="9" (python threadsbot.py report & set "pid=" & set /p "pid=Post number to delete: " & call python threadsbot.py delete %%pid%% & pause)
if "%s%"=="10" (python machine.py tiktok-status & pause)
if "%s%"=="11" (python threadsbot.py check & pause)
if "%s%"=="12" (winget install -e --id Gyan.FFmpeg & pause)
if "%s%"=="13" (set "DRY_RUN=0" & python machine.py ig-check & set "DRY_RUN=1" & pause)
if "%s%"=="14" (powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autopilot-setup.ps1" & pause)
if "%s%"=="0" goto menu
goto settings
