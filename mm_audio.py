"""
Audio: the bot's OWN AI music (ACE-Step 1.5, runs in your ComfyUI, MIT license, no copyright claims), music beds under
clips, slideshow reels with music + your cloned voice, and short AI jingles.

One-time install: app -> Settings -> "Install AI music (9.3 GB)" (or: python machine.py install-music).
That downloads ace_step_1.5_turbo_aio.safetensors (Comfy-Org, Hugging Face) into your ComfyUI models/checkpoints.

Library: media/audio/music/<mood>/*.flac (+ a .json with the prompt). The bot keeps ~6 tracks per mood, makes new ones
now and then, and learns which moods do best (brain dim "music_mood").
"""
import json
import os
import random
import time
import uuid

import requests

import mm_images
import mm_video

CKPT = "ace_step_1.5_turbo_aio.safetensors"
CKPT_URL = ("https://huggingface.co/Comfy-Org/ace_step_1.5_ComfyUI_files/resolve/main/checkpoints/"
            "ace_step_1.5_turbo_aio.safetensors")
MUSIC = os.path.join("media", "audio", "music")
MOODS = {
    "hype": "energetic trap beat, hard 808 bass, fast hi-hats, hype gaming montage, punchy drums",
    "funny": "goofy comedic bouncy tune, pizzicato strings, tuba, cartoon slapstick, playful",
    "chill": "chill lofi hip hop beat, mellow keys, vinyl crackle, relaxed groove",
    "dramatic": "epic dramatic orchestral, big drums, choir hits, cinematic tension, over the top",
    "spooky": "creepy suspense ambient, eerie synth pads, horror game, backrooms atmosphere",
    "retro": "8-bit chiptune video game music, upbeat, retro arcade, square wave melody",
}
TYPE_MOOD = {"funny": "funny", "fail": "funny", "clutch": "hype", "rage": "hype", "chaos": "hype",
             "wholesome": "chill", "story": "chill", "weird": "spooky"}
PER_MOOD = 6


def models_dir():
    import mm_safety
    return mm_safety.config().get("comfy_models_dir", r"B:\AI\Comfy UI\models")


def installed():
    return os.path.exists(os.path.join(models_dir(), "checkpoints", CKPT))


def install():
    """Download the AI music model (9.3 GB, resumable)."""
    dest = os.path.join(models_dir(), "checkpoints", CKPT)
    if os.path.exists(dest):
        return print("AI music model already installed.")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    print(f"Downloading the AI music model (ACE-Step 1.5, 9.3 GB) to {dest} ...", flush=True)
    with requests.get(CKPT_URL, stream=True, timeout=60, headers={"Range": f"bytes={have}-"} if have else {}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0)) + have
        done, last = have, time.time()
        with open(part, "ab" if have else "wb") as f:
            for chunk in r.iter_content(chunk_size=8 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 10:
                    print(f"  {done / 1e9:.1f} / {total / 1e9:.1f} GB", flush=True)
                    last = time.time()
    os.replace(part, dest)
    print("AI music model installed. Restart ComfyUI once so it sees it.")


def tracks(mood):
    d = os.path.join(MUSIC, mood)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d) if f.endswith((".flac", ".mp3", ".wav"))]


def generate(mood, seconds=40, extra="", lyrics="[Instrumental]", ollama_model=None):
    """Make one new track with ACE-Step in ComfyUI. Returns the file path."""
    base = mm_images.ensure_comfy()
    if not base:
        raise RuntimeError("ComfyUI isn't available")
    if not installed():
        raise RuntimeError("AI music model isn't installed (Settings -> Install AI music)")
    if ollama_model:
        mm_images.unload_ollama(ollama_model)
    tags = MOODS.get(mood, mood) + (", " + extra if extra else "") + ", instrumental, no vocals, loopable, high quality"
    seed = random.randint(1, 2**31)
    bpm = {"hype": 140, "funny": 110, "chill": 85, "dramatic": 100, "spooky": 80, "retro": 150}.get(mood, 120)
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "3": {"class_type": "TextEncodeAceStepAudio1.5", "inputs": {
            "clip": ["1", 1], "tags": tags, "lyrics": lyrics, "seed": seed, "bpm": bpm, "duration": float(seconds),
            "timesignature": "4", "language": "en", "keyscale": random.choice(["C major", "A minor", "E minor", "G major"]),
            "generate_audio_codes": True, "cfg_scale": 2.0, "temperature": 0.85, "top_p": 0.9, "top_k": 0, "min_p": 0.0}},
        "4": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}},
        "5": {"class_type": "EmptyAceStep1.5LatentAudio", "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "seed": seed, "steps": 8, "cfg": 1.0,
                                                   "sampler_name": "euler", "scheduler": "simple", "positive": ["3", 0],
                                                   "negative": ["4", 0], "latent_image": ["5", 0], "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudio", "inputs": {"audio": ["7", 0], "filename_prefix": "clip_music/track"}},
    }
    r = requests.post(f"{base}/prompt", json={"prompt": wf, "client_id": "clip-machine"}, timeout=60).json()
    if "prompt_id" not in r:
        raise RuntimeError(f"ComfyUI rejected the music workflow: {str(r)[:400]}")
    pid = r["prompt_id"]
    try:
        for _ in range(450):  # up to 15 min (first load of the model is the slow part)
            time.sleep(2)
            h = requests.get(f"{base}/history/{pid}", timeout=20).json().get(pid)
            if h and h.get("outputs", {}).get("8"):
                a = h["outputs"]["8"]["audio"][0]
                raw = requests.get(f"{base}/view", params={"filename": a["filename"], "subfolder": a["subfolder"],
                                                          "type": a["type"]}, timeout=60).content
                d = os.path.join(MUSIC, mood)
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, f"{mood}_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}.flac")
                with open(path, "wb") as f:
                    f.write(raw)
                with open(path + ".json", "w", encoding="utf-8") as f:
                    json.dump({"mood": mood, "tags": tags, "seconds": seconds, "seed": seed}, f)
                print(f"AI music: made a new {mood} track ({seconds}s)")
                return path
            if h and h.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"Music generation failed in ComfyUI: {str(h['status'])[:400]}")
        raise RuntimeError("Music generation took too long")
    finally:
        try:
            requests.post(f"{base}/free", json={"unload_models": True, "free_memory": True}, timeout=20)
        except Exception:
            pass


def pick(con, mood=None, ollama_model=None):
    """A track for this mood: makes a fresh one if the library is small (and the model is installed), else reuses one.
    Returns (path, mood) or (None, mood)."""
    if not mood:
        try:
            import mm_brain
            mood = mm_brain.choose(con, "music_mood", list(MOODS), log=False)
        except Exception:
            mood = random.choice(list(MOODS))
    have = tracks(mood)
    want_new = installed() and (len(have) < PER_MOOD or random.random() < 0.15)
    if want_new:
        try:
            return generate(mood, ollama_model=ollama_model), mood
        except Exception as e:
            print(f"[AI music skipped: {e}]")
    return (random.choice(have) if have else None), mood


def add_bed(video_in, track, video_out, music_db=-17, solo=False):
    """Music under a clip. solo=True (silent video): music at normal volume. Otherwise it ducks under speech."""
    ff = mm_video.ffmpeg_exe()
    if solo:
        mm_video.run([ff, "-y", "-i", video_in, "-stream_loop", "-1", "-i", track, "-filter_complex",
                      f"[1:a]volume=-3dB,afade=t=in:d=0.4[a]", "-map", "0:v", "-map", "[a]", "-shortest",
                      "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", video_out])
        return video_out
    mm_video.run([ff, "-y", "-i", video_in, "-stream_loop", "-1", "-i", track, "-filter_complex",
                  f"[1:a]volume={music_db}dB[m];[m][0:a]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=400[d];"
                  "[0:a][d]amix=inputs=2:duration=first:normalize=0[a]",
                  "-map", "0:v", "-map", "[a]", "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", video_out])
    return video_out


def slideshow(images, video_out, track=None, voice_text=None, seconds_each=2.6):
    """Pictures -> a 9:16 video with slow zooms, music, and (if installed) your cloned voice reading a line."""
    import mm_voice
    ff = mm_video.ffmpeg_exe()
    work = os.path.splitext(video_out)[0] + "_parts"
    os.makedirs(work, exist_ok=True)
    parts = []
    fr = 30
    n = int(seconds_each * fr)
    for i, img in enumerate(images):
        part = os.path.join(work, f"p{i}.mp4")
        zoom = "zoom+0.0012" if i % 2 == 0 else "if(eq(on,1),1.12,zoom-0.0012)"
        mm_video.run([ff, "-y", "-loop", "1", "-i", img, "-filter_complex",
                      "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
                      "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                      f"zoompan=z='{zoom}':d={n}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fr},setsar=1",
                      "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", part])
        parts.append(part)
    lst = os.path.join(work, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("".join(f"file '{os.path.abspath(p)}'\n" for p in parts))
    silent = os.path.join(work, "silent.mp4")
    mm_video.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                  "-map", "0:v", "-map", "1:a", "-shortest", "-c:v", "copy", "-c:a", "aac", silent])
    cur = silent
    if track:
        withm = os.path.join(work, "music.mp4")
        add_bed(cur, track, withm, solo=True)
        cur = withm
    if voice_text and mm_voice.available():
        try:
            voiced = os.path.join(work, "voice.mp4")
            mm_voice.add_hook(cur, voice_text, voiced)
            cur = voiced
        except Exception as e:
            print(f"[voice skipped on slideshow: {e}]")
    os.replace(cur, video_out)
    return video_out
