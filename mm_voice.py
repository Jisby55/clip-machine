"""
AI voice clone of the creator (Chatterbox, runs locally on the GPU in its own environment "voice_env").
Install once with INSTALL-VOICE.bat. The bot builds a ~25s voice sample from your own streams/videos
(voice_ref.wav; you can replace it with a cleaner recording of yourself any time).
Anything with the AI voice is labeled as AI on every platform.
"""
import os
import subprocess

import mm_video

import compat  # noqa: E402
ENV_PY = compat.venv_python("voice_env")
REF = "voice_ref.wav"


def installed():
    return os.path.exists(ENV_PY)


def available(con=None):
    if not installed():
        return False
    if not os.path.exists(REF) and con is not None:
        try:
            build_reference(con)
        except Exception as e:
            print(f"[voice sample not built yet: {e}]")
    return os.path.exists(REF)


def build_reference(con, seconds=25):
    """Glue together the talkiest bits of your own videos into a clean-ish voice sample."""
    rows = con.execute("SELECT m.start, m.end, m.text, s.path FROM mm_moments m JOIN mm_sources s ON s.video_id=m.video_id "
                       "WHERE s.path != '' AND m.text IS NOT NULL ORDER BY LENGTH(m.text) * 1.0 / (m.end - m.start + 1) DESC "
                       "LIMIT 12").fetchall()
    rows = [r for r in rows if r[3] and os.path.exists(r[3])]
    if not rows:
        raise RuntimeError("no downloaded videos with speech yet")
    ff, parts, total = mm_video.ffmpeg_exe(), [], 0.0
    os.makedirs("media", exist_ok=True)
    for i, (start, end, text, path) in enumerate(rows):
        dur = min(end - start, 10)
        part = os.path.join("media", f"_vref{i}.wav")
        # voice band only + light denoise so game audio matters less
        mm_video.run([ff, "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", path, "-vn", "-ac", "1", "-ar", "24000",
                      "-af", "highpass=f=90,lowpass=f=7500,afftdn=nf=-25,dynaudnorm", part])
        parts.append(part)
        total += dur
        if total >= seconds:
            break
    lst = os.path.join("media", "_vref.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
    mm_video.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-t", str(seconds), REF])
    for p in parts + [lst]:
        os.remove(p)
    print(f"Voice sample built from your videos: {REF} ({min(total, seconds):.0f}s)")


def speak(text, out_wav):
    """Your cloned voice saying `text` -> out_wav. Raises on failure."""
    r = subprocess.run([ENV_PY, "voice_tts.py", text[:300], REF, out_wav], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not os.path.exists(out_wav):
        raise RuntimeError("voice clone failed: " + (r.stderr or r.stdout)[-300:])
    return out_wav


def add_hook(video_in, text, video_out):
    """Put an AI-voice line (in your voice) over the first seconds of a clip, ducking the original audio."""
    wav = video_out + ".voice.wav"
    speak(text, wav)
    dur = mm_video.media_duration(wav) + 0.3
    mm_video.run([mm_video.ffmpeg_exe(), "-y", "-i", video_in, "-i", wav, "-filter_complex",
                  f"[0:a]volume='if(lt(t,{dur:.2f}),0.25,1)':eval=frame[a0];[1:a]apad[a1];"
                  "[a0][a1]amix=inputs=2:duration=first:normalize=0[a]",
                  "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", video_out])
    os.remove(wav)
    return video_out
