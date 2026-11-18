#!/bin/bash
# One-time setup of the Clip Machine on a Mac. Safe to re-run.
set -e
cd "$(dirname "$0")/.."
HERE="$(pwd)"
case "$HERE" in
  *CloudStorage*|*"Google Drive"*|*iCloud*|*/Desktop/*|*/Documents/*|*/Downloads/*)
    echo "!! Move this folder to your home folder first (for example ~/Clip-Machine), then run this again."
    echo "   Background jobs can't reliably read Desktop/Documents/Downloads/Drive/iCloud folders on a Mac."
    exit 1;;
esac
echo "== 1/5 Homebrew tools"
command -v brew >/dev/null || { echo 'Install Homebrew first: /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'; exit 1; }
brew install python@3.12 python-tk@3.12 ffmpeg-full deno cloudflared
# ffmpeg-full includes libass (burned-in captions); plain ffmpeg renders clips without captions
echo "== 2/5 Python environment (.venv)"
PY="$(brew --prefix)/bin/python3.12"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q -U pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -c "import tkinter" || { echo "Tkinter missing: brew reinstall python-tk@3.12"; exit 1; }
echo "== 3/5 Your settings files"
[ -f profile.json ] || { cp profile.example.json profile.json; echo "   Created profile.json - fill in your name, channel and handles."; }
[ -f machine_config.json ] || cp machine_config.example.json machine_config.json
[ -f secrets.env ] || { cp secrets.env.example secrets.env; chmod 600 secrets.env; echo "   Created secrets.env - paste your keys (or run: bash mac/set-keys.sh)."; }
echo "== 4/5 Folders"
mkdir -p media/source media/clips media/audio media/images media/shorts_audio media/thumbs
echo "== 5/5 AI models (Ollama)"
if command -v ollama >/dev/null; then
  MODEL=$(.venv/bin/python -c "import json;print(json.load(open('machine_config.json')).get('ollama_model','qwen3:8b'))")
  ollama pull "$MODEL" || echo "(couldn't pull $MODEL - open the Ollama app first, then re-run)"
  ollama pull nomic-embed-text || true
else
  echo "Ollama not found - install the Ollama app from ollama.com, open it once, then re-run this."
fi
echo
echo "DONE. Next: fill in profile.json and secrets.env (see README.md), then double-click 'Clip Machine.command'."
