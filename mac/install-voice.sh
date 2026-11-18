#!/bin/bash
# Optional: installs the Chatterbox voice clone (about 3-4 GB). Works on Apple Silicon, slower than the PC's GPU.
cd "$(dirname "$0")/.."
PY="$(brew --prefix)/bin/python3.12"
[ -x voice_env/bin/python ] || "$PY" -m venv voice_env
voice_env/bin/python -m pip install -U pip
voice_env/bin/python -m pip install torch torchaudio chatterbox-tts
voice_env/bin/python -c "import torch, chatterbox; print('Voice clone ready. MPS:', torch.backends.mps.is_available())"
