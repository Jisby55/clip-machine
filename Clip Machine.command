#!/bin/bash
# Double-click to open the Clip Machine app (first time: right-click > Open).
cd "$(dirname "$0")"
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec .venv/bin/python app.py
