"""
Upload your own video through the Clip Machine: it posts it to every platform you tick.
Leave the title and/or description empty and the bot writes them from what's IN the video (what's said, via Whisper,
and what's on screen, if your AI model can see pictures). Hashtags are added per platform like every other post.
TikTok still can't be posted automatically, so it lands in the app's TikTok tab with its caption ready.
"""
import base64
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

import requests

import mm_video

UP = os.path.join("media", "uploads")
LIMITS = {"facebook": 90, "youtube": 180, "instagram": 900, "tiktok": 600}  # seconds (YouTube: over 3 min = normal video)


def _probe(path):
    """(width, height, seconds)."""
    r = subprocess.run([mm_video.ffmpeg_exe(), "-i", path], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    import re
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    rot = re.search(r"rotate\s*:\s*(-?\d+)|rotation of (-?\d+)", r.stderr)
    if rot and abs(int(rot.group(1) or rot.group(2))) in (90, 270):
        w, h = h, w
    return w, h, mm_video.media_duration(path)


def make_vertical(src, out):
    """Wide video -> 9:16 with the video in the middle and a blurred copy filling the background."""
    mm_video.run([mm_video.ffmpeg_exe(), "-y", "-i", src, "-filter_complex",
                  "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:6[bg];"
                  "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
                  "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]", "-map", "[v]", "-map", "0:a?",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    return out


def _frames(path, secs, n=3):
    """A few stills from the video (for the AI to look at)."""
    outs = []
    for k in range(n):
        t = secs * (k + 1) / (n + 1)
        p = os.path.join(os.path.dirname(path), f"look{k}.jpg")
        subprocess.run([mm_video.ffmpeg_exe(), "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-vf", "scale=640:-2",
                        p], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.path.exists(p):
            outs.append(p)
    return outs


def describe_frames(frames, model):
    """Ask the local model what it sees (only works if the model can see pictures; otherwise returns '')."""
    if not frames:
        return ""
    try:
        imgs = [base64.b64encode(open(f, "rb").read()).decode() for f in frames]
        r = requests.post("http://localhost:11434/api/generate", timeout=300, json={
            "model": model, "stream": False, "think": False, "images": imgs, "keep_alive": "1m",
            "prompt": "These are stills from one short video, in order. In 2-3 plain sentences: what game or scene is "
                      "it, and what happens? Only describe what you can see.", "options": {"temperature": 0.2}})
        if r.status_code != 200:
            return ""
        return (r.json().get("response") or "").strip()[:600]
    except Exception:
        return ""


def prepare(con, src, title="", caption="", vertical=True):
    """Copy the video in, understand it, and register it as a clip. Returns the clip row."""
    import mm_safety
    import mm_words
    import threadsbot as tb
    if not os.path.exists(src):
        raise SystemExit(f"Can't find the video: {src}")
    work = os.path.join(UP, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(work, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or ".mp4"
    dest = os.path.join(work, "original" + ext)
    print(f"Copying {os.path.basename(src)}...", flush=True)
    shutil.copy2(src, dest)
    w, h, secs = _probe(dest)
    print(f"Video: {w}x{h}, {secs:.0f} seconds", flush=True)
    final = dest
    if vertical and w > h and secs <= 180:
        print("Making a vertical (9:16) version for Shorts / Reels / TikTok...", flush=True)
        final = make_vertical(dest, os.path.join(work, "vertical.mp4"))
    elif ext != ".mp4":
        final = os.path.join(work, "video.mp4")
        mm_video.run([mm_video.ffmpeg_exe(), "-y", "-i", dest, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", final])
    said, seen = "", ""
    if not (title and caption):  # the AI needs to know what's in it
        print("Listening to what's said (Whisper)...", flush=True)
        try:
            said = " ".join(s.text.strip() for s in mm_video.transcribe(dest, "small", vad_filter=True))[:3000]
        except Exception as e:
            print(f"[couldn't transcribe: {e}]")
        print("Looking at the video...", flush=True)
        seen = describe_frames(_frames(final, secs), mm_safety.config().get("ollama_model") or tb.MODEL)
        if seen:
            print(f"It sees: {seen}")
    text = " | ".join(x for x in (f"Your own upload. Title idea: {title}" if title else "Your own uploaded video",
                                  f"Description: {caption}" if caption else "", f"On screen: {seen}" if seen else "",
                                  f"Said: {said}" if said else "") if x)
    cur = con.execute("INSERT INTO mm_moments (video_id, start, end, text, loudness, ai_score, hook, title, used) "
                      "VALUES ('upload', 0, ?, ?, 0, 10, '', ?, 1)", (secs, text, title or ""))
    mid = cur.lastrowid
    v = {"type": "upload", "user_title": title.strip() or None, "user_caption": caption.strip() or None,
         "seconds": round(secs, 1), "original": os.path.abspath(src)}
    cur = con.execute("INSERT INTO mm_clips (moment_id, path, variant, hook, title, created_at, status) "
                      "VALUES (?,?,?,?,?,?,'upload')", (mid, final, json.dumps(v), "", title or "your upload",
                                                         datetime.now(timezone.utc).isoformat()))
    con.commit()
    if mm_words.is_sexual(f"{title} {caption}"):
        print("Heads up: your title/description has words the platforms hold back; posting it as you wrote it.")
    return con.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?",
                       (cur.lastrowid,)).fetchone(), secs


def post_everywhere(con, clip, secs, platforms):
    """Post one uploaded clip to each platform you picked. Returns a summary line per platform."""
    import machine as M
    out = []
    for p in platforms:
        if secs > LIMITS.get(p, 10 ** 6) and p != "youtube":
            out.append(f"{p}: skipped (too long: {p.title()} allows {LIMITS[p]}s)")
            continue
        t0 = time.time()
        try:
            if p == "youtube":
                M.post_youtube(con, force=True, clip=clip)
            elif p == "instagram":
                M.post_instagram(con, force=True, kind="reel", clip=clip)
            elif p == "facebook":
                M.post_facebook(con, force=True, clip=clip)
            elif p == "tiktok":
                M.post_tiktok(con, force=True, clip=clip)
            done = con.execute("SELECT 1 FROM mm_posts WHERE clip_id=? AND platform=?", (clip[0], p)).fetchone()
            out.append(f"{p}: {'done' if done else 'NOT posted (see above)'} ({time.time() - t0:.0f}s)")
        except (SystemExit, Exception) as e:
            out.append(f"{p}: failed ({str(e)[:160]})")
    return out
