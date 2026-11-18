#!/bin/bash
# One autopilot cycle (LIVE): Threads + video factory + posting. Output -> bot.log. launchd runs this every 15 minutes.
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export DRY_RUN=0 PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
export BOT_MODEL="${BOT_MODEL:-qwen3:8b}"
.venv/bin/python machine.py tick >> bot.log 2>&1
