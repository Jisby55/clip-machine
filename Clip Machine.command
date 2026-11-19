#!/bin/bash
# Double-click to open the Clip Machine app (first time: right-click > Open).
cd "$(dirname "$0")"
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ ! -x .venv/bin/python ]; then
  echo "First time? The Clip Machine needs a one-time setup."
  read -r -p "Press Enter to run it now (or Ctrl+C to stop)... "
  bash mac/setup_mac.sh || { read -r -p "Setup stopped (see above). Press Enter to close."; exit 1; }
fi
exec .venv/bin/python app.py
