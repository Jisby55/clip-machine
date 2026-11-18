@echo off
cd /d "%~dp0.."
echo Installing the voice clone (Chatterbox) in its own folder "voice_env".
echo This downloads about 3-4 GB (PyTorch + the voice model). It only needs to happen once.
echo.
if not exist voice_env\Scripts\python.exe python -m venv voice_env
voice_env\Scripts\python -m pip install --upgrade pip
voice_env\Scripts\python -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
voice_env\Scripts\python -m pip install chatterbox-tts
echo.
echo Testing the voice (first run downloads the voice model)...
voice_env\Scripts\python -c "import torch, chatterbox; print('Voice clone ready. GPU:', torch.cuda.is_available())"
echo.
echo Done. The bot builds your voice sample from your videos automatically (voice_ref.wav).
echo Want it to sound more like you? Record 20-30 seconds of you talking clearly and save it as voice_ref.wav here.
pause
