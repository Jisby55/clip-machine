"""Runs inside voice_env (Chatterbox). Usage: python voice_tts.py "text" reference.wav out.wav"""
import sys

import torch
import torchaudio
from chatterbox.tts import ChatterboxTTS

text, ref, out = sys.argv[1], sys.argv[2], sys.argv[3]
dev = "cuda" if torch.cuda.is_available() else ("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu")
try:
    model = ChatterboxTTS.from_pretrained(device=dev)
except Exception:
    dev = "cpu"
    model = ChatterboxTTS.from_pretrained(device=dev)
wav = model.generate(text, audio_prompt_path=ref, exaggeration=0.65, cfg_weight=0.45)
torchaudio.save(out, wav, model.sr)
print("ok", dev)
