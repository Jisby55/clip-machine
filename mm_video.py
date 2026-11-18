"""
Video factory: turns the creator's own YouTube videos/streams into vertical Shorts/TikToks.

1. sync_sources()   works through the WHOLE channel (streams + least-viewed videos first), a few at a time;
                    skips moments that match Shorts the creator already made themselves
2. analyze()        transcript (YouTube captions, else local Whisper) + loudness curve
3. find_moments()   loud peaks + transcript windows, then the local AI rates them and writes a hook/title
4. render()         9:16 edit; the "variant" (captions, punch-in zooms, hook text, layout, length)
                    is chosen by a bandit that learns which styles score best
"""
import json
import math
import os
import random
import re
import shutil
import subprocess
import wave
from datetime import datetime, timezone

import mm_safety

MEDIA = os.path.join("media")
SRC = os.path.join(MEDIA, "source")
OUT = os.path.join(MEDIA, "clips")
CAPTION_FONT = "Impact"

VARIANT_OPTIONS = {
    "captions": ["karaoke", "single"],  # both word-by-word: a 3-word line with the spoken word lit up, or one big word
    "zoom": ["punch", "none"],
    "hook": ["text", "none"],
    "layout": ["face", "blur", "crop"],  # face = facecam on top + gameplay below (or follow the face); falls back to blur
    "length": ["short", "long"],  # short ~20-30s, long ~35-55s
    "voice": ["ai_hook", "none"],  # AI-voice line in your cloned voice at the start (if the voice clone is installed)
    "music": ["bed", "none"],  # the bot's own AI music under the clip, ducked under your voice (if AI music is installed)
}


# ---------- helpers ----------
def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd[:6]))}...\n{r.stderr[-1500:]}")
    return r


def has_filter(name):
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return f" {name} " in out


def media_duration(path):
    r = subprocess.run([ffmpeg_exe(), "-i", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS mm_sources (
        video_id TEXT PRIMARY KEY, title TEXT, path TEXT, duration REAL, views INTEGER,
        is_stream INTEGER, analyzed INTEGER DEFAULT 0, added_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS mm_moments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT, start REAL, end REAL, text TEXT,
        loudness REAL, ai_score REAL, hook TEXT, title TEXT, used INTEGER DEFAULT 0)""")
    for col in ("subscores TEXT", "mtype TEXT", "signals TEXT"):
        try:
            con.execute(f"ALTER TABLE mm_moments ADD COLUMN {col}")
        except Exception:
            pass
    con.execute("""CREATE TABLE IF NOT EXISTS mm_clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT, moment_id INTEGER, path TEXT, variant TEXT,
        hook TEXT, title TEXT, description TEXT, ai_voice INTEGER DEFAULT 0, ai_visual INTEGER DEFAULT 0,
        created_at TEXT, status TEXT DEFAULT 'ready')""")
    for col in ("approved INTEGER DEFAULT 0", "hold_reason TEXT"):
        try:
            con.execute(f"ALTER TABLE mm_clips ADD COLUMN {col}")
        except Exception:
            pass
    con.execute("""CREATE TABLE IF NOT EXISTS mm_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id INTEGER, platform TEXT, remote_id TEXT,
        posted_at TEXT, privacy TEXT, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
        retention REAL, score REAL, checked_at TEXT)""")
    con.commit()


# ---------- 1. sources: the WHOLE channel ----------
def ensure_library(con):
    con.execute("""CREATE TABLE IF NOT EXISTS mm_library (
        video_id TEXT PRIMARY KEY, title TEXT, duration REAL, views INTEGER, is_stream INTEGER, indexed_at TEXT)""")
    try:
        con.execute("ALTER TABLE mm_library ADD COLUMN priority INTEGER DEFAULT 0")
    except Exception:
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS mm_existing_shorts (
        video_id TEXT PRIMARY KEY, title TEXT, text TEXT, checked INTEGER DEFAULT 0)""")
    con.commit()


def index_channel(con, force=False):
    """List EVERY long video, stream and Short on the main channel (metadata only, no downloads). Once a day."""
    import yt_dlp
    from datetime import timedelta

    ensure_library(con)
    row = con.execute("SELECT value FROM meta WHERE key='library_indexed_at'").fetchone()
    if row and not force and datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) < timedelta(hours=24):
        return 0
    cfg = mm_safety.config()
    now_s = datetime.now(timezone.utc).isoformat()
    n = 0
    for tab in ("videos", "streams", "shorts"):
        url = f"https://www.youtube.com/channel/{cfg['source_channel_id']}/{tab}"
        try:
            with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True, "ignoreerrors": True}) as y:
                info = y.extract_info(url, download=False) or {}
        except Exception as ex:
            print(f"[channel index failed for {tab}] {ex}")
            continue
        for e in info.get("entries") or []:
            if not e or not e.get("id"):
                continue
            if tab == "shorts":
                con.execute("INSERT OR IGNORE INTO mm_existing_shorts (video_id, title) VALUES (?,?)",
                            (e["id"], e.get("title")))
            elif (e.get("duration") or 999) >= 90:
                con.execute("INSERT INTO mm_library (video_id, title, duration, views, is_stream, indexed_at) "
                            "VALUES (?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, "
                            "duration=excluded.duration, views=excluded.views, indexed_at=excluded.indexed_at",
                            (e["id"], e.get("title"), e.get("duration"), e.get("view_count") or 0,
                             1 if tab == "streams" else 0, now_s))  # keeps your stars / blocks (priority)
                n += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('library_indexed_at', ?)", (now_s,))
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM mm_library").fetchone()[0]
    shorts = con.execute("SELECT COUNT(*) FROM mm_existing_shorts").fetchone()[0]
    print(f"Channel library: {total} long videos/streams, {shorts} existing Shorts")
    return n


def check_new_uploads(con):
    """Hourly: look at the newest few videos/streams on the channel. A NEW one jumps to the front of the line so
    it gets clipped within the hour (fresh content does best). Streams count once they've ended."""
    import yt_dlp
    from datetime import timedelta

    ensure_library(con)
    try:
        con.execute("ALTER TABLE mm_library ADD COLUMN priority INTEGER DEFAULT 0")
        con.commit()
    except Exception:
        pass
    row = con.execute("SELECT value FROM meta WHERE key='new_uploads_at'").fetchone()
    if row and datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) < timedelta(hours=1):
        return []
    cfg = mm_safety.config()
    known = {r[0] for r in con.execute("SELECT video_id FROM mm_library")}
    first_run = not known
    fresh = []
    for tab in ("videos", "streams"):
        try:
            with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True, "playlistend": 6, "ignoreerrors": True}) as y:
                info = y.extract_info(f"https://www.youtube.com/channel/{cfg['source_channel_id']}/{tab}", download=False) or {}
        except Exception as ex:
            print(f"[new-upload check failed for {tab}] {ex}")
            continue
        for e in info.get("entries") or []:
            if not e or not e.get("id") or e["id"] in known:
                continue
            if e.get("live_status") in ("is_live", "is_upcoming") or not e.get("duration"):
                continue  # stream still live / not processed yet: catch it next hour
            if e["duration"] < 90:
                continue
            con.execute("INSERT OR REPLACE INTO mm_library (video_id, title, duration, views, is_stream, indexed_at, priority) "
                        "VALUES (?,?,?,?,?,?,?)", (e["id"], e.get("title"), e["duration"], e.get("view_count") or 0,
                                                    1 if tab == "streams" else 0, datetime.now(timezone.utc).isoformat(),
                                                    0 if first_run else 1))
            fresh.append(e.get("title") or e["id"])
    con.execute("INSERT OR REPLACE INTO meta VALUES ('new_uploads_at', ?)", (datetime.now(timezone.utc).isoformat(),))
    con.commit()
    if fresh and not first_run:
        print("NEW upload(s) found, clipping them first: " + "; ".join(fresh))
        try:
            import mm_notify
            mm_notify.send("New upload spotted", "Clipping it now: " + "; ".join(fresh)[:300], tags=["scissors"])
        except Exception:
            pass
    return fresh


def learn_existing_shorts(con, k=6):
    """Transcribe a few of the creator's OWN Shorts per run, so the bot never re-makes a clip they already made."""
    import yt_dlp

    ensure_library(con)
    todo = con.execute("SELECT video_id FROM mm_existing_shorts WHERE checked=0 LIMIT ?", (k,)).fetchall()
    if not todo:
        return 0
    tmp = os.path.join(MEDIA, "shorts_audio")
    os.makedirs(tmp, exist_ok=True)
    done = 0
    for (vid,) in todo:
        text = ""
        try:
            opts = {"format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": os.path.join(tmp, f"{vid}.%(ext)s"),
                    "quiet": True, "ffmpeg_location": ffmpeg_exe()}
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(f"https://www.youtube.com/shorts/{vid}", download=True)
                path = y.prepare_filename(info)
            text = " ".join(s.text.strip() for s in transcribe(path, "base"))
            os.remove(path)
        except Exception as ex:
            print(f"[couldn't read your Short {vid}: {type(ex).__name__}]")
        con.execute("UPDATE mm_existing_shorts SET text=?, checked=1 WHERE video_id=?", (text, vid))
        con.commit()
        done += 1
    left = con.execute("SELECT COUNT(*) FROM mm_existing_shorts WHERE checked=0").fetchone()[0]
    print(f"Learned {done} of your existing Shorts ({left} left to learn)")
    return done


def _grams(text, n=5):
    w = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def already_made(con, text):
    """True if this moment's words overlap one of the creator's existing Shorts (they already clipped it)."""
    mine = _grams(text)
    if len(mine) < 3:
        return False
    ensure_library(con)
    for (t,) in con.execute("SELECT text FROM mm_existing_shorts WHERE text IS NOT NULL AND text != ''"):
        if len(mine & _grams(t)) >= 3:
            return True
    return False


def cleanup_sources(con):
    """Free disk space: delete downloaded source videos whose good moments are all used up."""
    if not mm_safety.config().get("delete_used_sources", True):
        return
    rows = con.execute("SELECT video_id, path FROM mm_sources WHERE analyzed=1").fetchall()
    for vid, path in rows:
        left = con.execute("SELECT COUNT(*) FROM mm_moments WHERE video_id=? AND used=0 AND ai_score>=6 "
                           "AND COALESCE(pic_used,0)=0", (vid,)).fetchone()[0]
        if left == 0 and path and os.path.exists(path):
            try:
                os.remove(path)
                print(f"Freed space: deleted used-up source {vid}")
            except OSError:
                pass


def keep_sources_flowing(con, ask):
    """Every run: keep at least `active_sources` DIFFERENT videos/streams with unused good moments.
    Downloads more when it's low and analyzes one new video per run (analysis is the heavy part)."""
    cfg = mm_safety.config()
    active = con.execute("SELECT COUNT(DISTINCT video_id) FROM mm_moments WHERE used=0 AND ai_score>=6").fetchone()[0]
    waiting = con.execute("SELECT COUNT(*) FROM mm_sources WHERE analyzed=0").fetchone()[0]
    want = cfg.get("active_sources", 6)
    have = {r[0] for r in con.execute("SELECT video_id FROM mm_sources")}
    try:
        new_ones = [r[0] for r in con.execute("SELECT video_id FROM mm_library WHERE priority>=1") if r[0] not in have]
    except Exception:
        new_ones = []
    if new_ones:
        sync_sources(con, downloads=min(len(new_ones), 2))
    elif active + waiting < want:
        sync_sources(con, downloads=min(cfg.get("downloads_per_run", 3), want - active - waiting))
    todo = con.execute("SELECT video_id, title FROM mm_sources WHERE analyzed=0 LIMIT 1").fetchone()
    if todo:
        print(f"Finding moments in: {todo[1]}", flush=True)
        try:
            find_moments(con, todo[0], ask)
        except Exception as e:
            print(f"[analysis failed for {todo[0]}: {e}; skipping this video]")
            con.execute("UPDATE mm_sources SET analyzed=1 WHERE video_id=?", (todo[0],))
            con.commit()
    print(f"Sources: {active} different videos/streams with good moments ready, {waiting} waiting to be analyzed")


def sync_sources(con, limit=None, downloads=None):
    """Download the next videos from the WHOLE channel: brand-new uploads first, then RANDOM videos and livestreams
    (alternating), never Shorts. Keeps only a few downloaded at a time."""
    import yt_dlp

    cfg = mm_safety.config()
    index_channel(con)
    try:
        con.execute("ALTER TABLE mm_moments ADD COLUMN pic_used INTEGER DEFAULT 0")
    except Exception:
        pass
    cleanup_sources(con)
    os.makedirs(SRC, exist_ok=True)
    on_disk = sum(1 for (p,) in con.execute("SELECT path FROM mm_sources") if p and os.path.exists(p))
    room = max(0, cfg.get("max_downloaded_sources", 12) - on_disk)
    downloads = min(room, downloads or cfg.get("downloads_per_run", 2))
    if downloads <= 0:
        return 0
    have = {r[0] for r in con.execute("SELECT video_id FROM mm_sources")}
    try:
        con.execute("ALTER TABLE mm_library ADD COLUMN priority INTEGER DEFAULT 0")
    except Exception:
        pass
    pri = [r for r in con.execute("SELECT video_id, title, views, is_stream FROM mm_library WHERE priority>=1 "
                                   "ORDER BY priority DESC").fetchall()
           if r[0] not in have]
    rows = [] if cfg.get("source_mode", "random") == "picked" else con.execute(  # blocked (-1) are never used
        "SELECT video_id, title, views, is_stream FROM mm_library WHERE COALESCE(priority,0)=0").fetchall()
    shorts = {r[0] for r in con.execute("SELECT video_id FROM mm_existing_shorts")}
    rows = [r for r in rows if r[0] not in have and r[0] not in shorts]
    # RANDOM picks from the whole channel, alternating livestreams and regular videos (never Shorts)
    streams = [r for r in rows if r[3]]
    videos = [r for r in rows if not r[3]]
    random.shuffle(streams)
    random.shuffle(videos)
    if random.random() < 0.5:
        streams, videos = videos, streams
    mixed = []
    while streams or videos:
        for lst in (streams, videos):
            if lst:
                mixed.append(lst.pop(0))
    picks = [{"id": r[0], "title": r[1], "view_count": r[2], "_stream": bool(r[3])} for r in pri + mixed]
    if not picks:
        print("Every video on the channel has been used. New uploads get picked up automatically.")
    added = 0
    for e in picks[:downloads]:
        vid = e["id"]
        opts = {"format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]",
                "outtmpl": os.path.join(SRC, f"{vid}.%(ext)s"), "merge_output_format": "mp4",
                "quiet": True, "noprogress": True, "ffmpeg_location": ffmpeg_exe()}
        try:
            print(f"Downloading source: {e.get('title')}", flush=True)
            with yt_dlp.YoutubeDL(opts) as y:
                y.download([f"https://www.youtube.com/watch?v={vid}"])
            path = os.path.join(SRC, f"{vid}.mp4")
            if media_duration(path) < 90:  # a Short slipped through: never clip Shorts
                print(f"[{vid} is a Short; skipping it]")
                try:
                    os.remove(path)
                except OSError:
                    pass
                con.execute("INSERT OR IGNORE INTO mm_existing_shorts (video_id, title) VALUES (?,?)", (vid, e.get("title")))
                con.commit()
                continue
            con.execute("INSERT OR REPLACE INTO mm_sources VALUES (?,?,?,?,?,?,0,?)",
                        (vid, e.get("title"), path, media_duration(path), e.get("view_count") or 0,
                         1 if e["_stream"] else 0, datetime.now(timezone.utc).isoformat()))
            con.commit()
            added += 1
        except Exception as ex:
            print(f"[download failed {vid}] {ex}")
            # don't retry it forever (members-only, removed, age-gated...)
            con.execute("INSERT OR REPLACE INTO mm_sources VALUES (?,?,?,?,?,?,1,?)",
                        (vid, e.get("title"), "", 0, e.get("view_count") or 0, 1 if e["_stream"] else 0,
                         datetime.now(timezone.utc).isoformat()))
            con.commit()
    return added


# ---------- 2. analysis ----------
def transcript_segments(video_id, path):
    """[(start, end, text)]. YouTube captions first (fast, no GPU); local Whisper as fallback."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        t = YouTubeTranscriptApi().fetch(video_id)
        return [(s.start, s.start + s.duration, s.text) for s in t]
    except Exception as ex:
        print(f"[no YouTube transcript for {video_id}: {type(ex).__name__}; using Whisper]")
    return [(s.start, s.end, s.text.strip()) for s in transcribe(path, "small", vad_filter=True)]


_whisper = {}
_cpu_only = False


def _add_nvidia_dlls():
    """Windows: let Whisper find cuBLAS/cuDNN from the pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12."""
    if os.name != "nt":
        return
    import site
    roots = site.getsitepackages() + [site.getusersitepackages()]
    for root in roots:
        nv = os.path.join(root, "nvidia")
        if not os.path.isdir(nv):
            continue
        for pkg in os.listdir(nv):
            b = os.path.join(nv, pkg, "bin")
            if os.path.isdir(b):
                try:
                    os.add_dll_directory(b)
                except OSError:
                    pass
                os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def whisper_model(size="small"):
    """GPU Whisper if CUDA/cuDNN work on this PC, otherwise CPU (slower but always works)."""
    _add_nvidia_dlls()
    from faster_whisper import WhisperModel
    key = (size, _cpu_only)
    if key not in _whisper:
        if _cpu_only:
            _whisper[key] = WhisperModel(size, device="cpu", compute_type="int8")
        else:
            try:
                _whisper[key] = WhisperModel(size, device="cuda", compute_type="float16")
            except Exception:
                _whisper[key] = WhisperModel(size, device="cpu", compute_type="int8")
    return _whisper[key]


def transcribe(path, size, **kw):
    """Run Whisper; if the GPU path dies mid-way (missing cuDNN DLLs is common on Windows), redo on CPU."""
    global _cpu_only
    try:
        segs, _ = whisper_model(size).transcribe(path, **kw)
        return list(segs)
    except Exception as e:
        if _cpu_only:
            raise
        print(f"[GPU Whisper failed ({type(e).__name__}); switching to CPU]")
        _cpu_only = True
        segs, _ = whisper_model(size).transcribe(path, **kw)
        return list(segs)


def loudness_curve(path, step=0.5):
    """RMS loudness every `step` seconds (mono 8 kHz)."""
    import numpy as np

    wav = path + ".8k.wav"
    if not os.path.exists(wav):
        run([ffmpeg_exe(), "-y", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-sample_fmt", "s16", wav])
    with wave.open(wav, "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    n = int(8000 * step)
    frames = len(data) // n
    rms = np.sqrt((data[: frames * n].reshape(frames, n) ** 2).mean(axis=1) + 1e-9)
    db = 20 * np.log10(rms + 1e-9)
    os.remove(wav)
    return db, step


def motion_curve(path, step=1.0):
    """How much the picture changes every second (fights, chases, explosions, scene cuts). Decodes only keyframes at
    a tiny size, so even a 3-hour stream takes a minute or two. Returns (values, step) or (None, step)."""
    import numpy as np
    w, h = 32, 18
    try:
        p = subprocess.run([ffmpeg_exe(), "-v", "error", "-skip_frame", "nokey", "-i", path, "-an",
                            "-vf", f"fps={1 / step},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=1800)
        frames = np.frombuffer(p.stdout, dtype=np.uint8)
        n = len(frames) // (w * h)
        if n < 10:
            return None, step
        f = frames[: n * w * h].reshape(n, w * h).astype(np.float32)
        diff = np.abs(np.diff(f, axis=0)).mean(axis=1)
        return np.concatenate([[0.0], diff]), step
    except Exception as e:
        print(f"[motion analysis skipped: {type(e).__name__}]")
        return None, step


HYPE = re.compile(r"\b(no way|let'?s go+|oh my god|omg|wtf|what the|bro+|dude|holy|lmao+|lol|haha+|ha ha|clutch|"
                  r"i'?m dead|he'?s dead|run+|get out|why+|nooo+|yes+|insane|oh no|wait wait|help)\b", re.I)
MOMENT_TYPES = ("funny", "fail", "clutch", "rage", "chaos", "wholesome", "story", "weird")


def _z(x):
    import numpy as np
    x = np.asarray(x, dtype=np.float32)
    return (x - np.median(x)) / (np.std(x) + 1e-6)


def find_moments(con, video_id, ask):
    """Offline highlight detection, all on this PC. Each half-second gets an excitement score from:
    LOUD (voice/game audio spikes), SUDDEN (loudness jumping up = reactions), MOTION (the picture going crazy),
    TALK (fast talking) and HYPE words ("no way", "let's go", "bro", laughing). The best peaks become candidates.
    Then the AI rates each one on hook / engagement / value / shareability and labels its type (funny, fail, clutch,
    rage, chaos, wholesome, story, weird) so the clips stay varied."""
    import numpy as np

    path, title = con.execute("SELECT path, title FROM mm_sources WHERE video_id=?", (video_id,)).fetchone()
    segs = transcript_segments(video_id, path)
    db, step = loudness_curve(path)
    if len(db) < 60:
        return 0
    n = len(db)
    loud = _z(db)
    rise = _z(np.maximum(0, db - np.convolve(db, np.ones(6) / 6, mode="same")))  # jump vs the last ~3 seconds
    mot, mstep = motion_curve(path)
    motion = np.zeros(n)
    if mot is not None:
        motion = _z(np.interp(np.arange(n) * step, np.arange(len(mot)) * mstep, mot))
    talk, hype = np.zeros(n), np.zeros(n)
    for s0, s1, txt in segs:
        a, b = int(s0 / step), max(int(s0 / step) + 1, int(s1 / step))
        if a >= n:
            continue
        talk[a:b] = len(txt.split()) / max(s1 - s0, 0.5)
        hype[a:b] += len(HYPE.findall(txt))
    talk, hype = _z(talk), np.clip(hype, 0, 3)
    excite = loud + 0.6 * rise + 0.6 * motion + 0.3 * talk + 0.5 * hype
    excite = np.convolve(excite, np.ones(3) / 3, mode="same")
    lo, hi = mm_safety.config().get("clip_seconds", [20, 55])
    peaks = []
    for i in np.argsort(-excite):
        t = i * step
        if excite[i] < 1.0 or len(peaks) >= 30:
            break
        if all(abs(t - p) > hi for p in peaks):
            peaks.append(t)
    cands, skipped = [], 0
    for p in peaks:
        start, end = max(0, p - random.uniform(10, 18)), p + random.uniform(8, hi - 18)
        text = " ".join(s[2] for s in segs if s[1] > start and s[0] < end)
        inside = [s for s in segs if s[1] > start and s[0] < end]
        if inside:  # snap to sentence boundaries
            start, end = inside[0][0], inside[-1][1]
        if end - start < lo:
            end = start + lo
        end = min(end, start + hi)
        if already_made(con, text):  # the creator already made a Short of this exact bit
            skipped += 1
            continue
        k = int(p / step)
        sig = {"loud": round(float(loud[k]), 1), "sudden": round(float(rise[k]), 1), "motion": round(float(motion[k]), 1),
               "talk": round(float(talk[k]), 1), "hype": int(hype[k])}
        cands.append((start, end, text, float(excite[k]), sig))
    if skipped:
        print(f"Skipped {skipped} moment(s) you already made Shorts of")
    if not cands:
        con.execute("UPDATE mm_sources SET analyzed=1 WHERE video_id=?", (video_id,))
        con.commit()
        return 0
    listing = "\n".join(f"{i}. [{c[1]-c[0]:.0f}s | signals: {c[4]}] {c[2][:380]}" for i, c in enumerate(cands))
    learned = ""
    try:  # what has actually kept people watching so far
        import mm_brain
        learned = "\n".join(x for x in (mm_brain.watch_summary(con), mm_brain.arm_summary(con, ("moment_type",))) if x)
    except Exception:
        pass
    raw = ask(
        "You pick the best moments from a gaming/comedy YouTuber's video or livestream to turn into Shorts/TikToks. "
        f"Video title: {title}\nCandidate moments (signals: loud = audio spike, sudden = reaction, motion = the screen "
        f"going crazy, talk = fast talking, hype = hype words; then the transcript):\n{listing}\n\n"
        + (f"WHAT YOUR AUDIENCE ACTUALLY WATCHES (use it):\n{learned}\n\n" if learned else "") +
        "Rate EACH candidate 1-10 on: HOOK (do the first 2 seconds grab you?), ENGAGEMENT (will people watch to the end "
        "and comment?), VALUE (funny/skillful/interesting payoff without needing context), SHARE (would someone send "
        f"it to a friend?). Label its TYPE: one of {', '.join(MOMENT_TYPES)}. Write a short dumb sarcastic on-screen "
        "HOOK (max 8 words) and a TITLE (max 60 chars, no hashtags). Reply ONLY a JSON array: "
        '[{"i":0,"hook_s":7,"eng":6,"value":7,"share":5,"type":"funny","hook":"...","title":"..."}]', temperature=0.4)
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        ratings = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        ratings = []
    by_i = {r.get("i"): r for r in ratings if isinstance(r, dict)}

    def num(r, k):
        try:
            return max(0.0, min(10.0, float(r.get(k, 0) or 0)))
        except (TypeError, ValueError):
            return 0.0
    for i, (s, e, text, ex, sig) in enumerate(cands):
        r = dict(by_i.get(i, {}))
        for k in ("type", "title", "hook"):  # the AI sometimes returns numbers here
            if r.get(k) is not None and not isinstance(r[k], str):
                r[k] = str(r[k])
        sub = {"hook": num(r, "hook_s"), "engagement": num(r, "eng"), "value": num(r, "value"), "share": num(r, "share")}
        score = 0.35 * sub["hook"] + 0.30 * sub["engagement"] + 0.15 * sub["value"] + 0.20 * sub["share"]
        if not any(sub.values()):
            score = num(r, "score")
        mtype = (r.get("type") or "").lower() if (r.get("type") or "").lower() in MOMENT_TYPES else "funny"
        import mm_words
        if mm_words.is_sexual(text) or mm_words.is_sexual(r.get("title", "")) or mm_words.is_sexual(r.get("hook", "")):
            score -= 2.5  # sexual stuff in the clip: much less likely to be picked (and titles never mention it)
        con.execute("INSERT INTO mm_moments (video_id, start, end, text, loudness, ai_score, hook, title, subscores, "
                    "mtype, signals) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (video_id, s, e, text, ex, round(score, 2), (r.get("hook") or "")[:80], (r.get("title") or title)[:90],
                     json.dumps(sub), mtype, json.dumps(sig)))
    con.execute("UPDATE mm_sources SET analyzed=1 WHERE video_id=?", (video_id,))
    con.commit()
    return len(cands)


# ---------- 3. variant bandit ----------
def choose_variant(con, explore=0.3):
    """Each style option (hook style, captions, zoom...) is picked by the brain: discounted Thompson sampling on
    how clips with that style did vs your usual. Proven styles win most; unproven ones still get tested."""
    try:
        import mm_brain
        out = {}
        for k, vals in VARIANT_OPTIONS.items():
            opts = {str(v): v for v in vals}
            out[k] = opts[mm_brain.choose(con, f"variant:{k}", list(opts), log=False)]
        return out
    except Exception as e:
        print(f"[brain unavailable for clip style, random: {e}]")
        return {k: random.choice(v) for k, v in VARIANT_OPTIONS.items()}


# ---------- 4. render ----------
def _ass_time(t):
    t = max(t, 0)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _ass_text(t):
    return (t or "").strip().upper().replace("{", "").replace("}", "").replace("\\", "")


def write_ass(path, words, hook=None, hook_secs=2.8, style="karaoke", margin_v=520):
    """WORD-BY-WORD captions synced to the speech.
    karaoke: a short line of up to 3 words where the word being said lights up yellow and pops bigger.
    single:  one big word at a time that pops in.
    Plus the optional hook text at the top for the first ~3 seconds."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{CAPTION_FONT},88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,2,60,60,{margin_v},1
Style: Big,{CAPTION_FONT},124,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,8,3,2,60,60,{margin_v},1
Style: Hook,{CAPTION_FONT},78,&H0000FFFF,&H0000FFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,2,8,60,60,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(hook_secs)},Hook,,0,0,0,,{_ass_text(hook)}")
    words = [(a, b, _ass_text(w)) for a, b, w in words if _ass_text(w)]
    if style == "single":
        for k, (a, b, w) in enumerate(words):
            end = max(b, a + 0.18)
            if k + 1 < len(words):
                end = min(max(end, words[k + 1][0]), words[k + 1][0] + 0.01)
            lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(end)},Big,,0,0,0,,"
                         f"{{\\fscx80\\fscy80\\t(0,90,\\fscx100\\fscy100)}}{w}")
    else:
        groups, cur = [], []
        for w in words:  # lines of up to 3 words, broken at punctuation / pauses
            if cur and (len(cur) == 3 or w[0] - cur[-1][1] > 0.6):
                groups.append(cur)
                cur = []
            cur.append(w)
            if w[2].endswith((".", "!", "?", ",")):
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        for g in groups:
            for k, (a, b, _) in enumerate(g):
                end = g[k + 1][0] if k + 1 < len(g) else max(b, a + 0.2)
                txt = " ".join((f"{{\\c&H00FFFF&\\fscx115\\fscy115}}{x[2]}{{\\r}}" if j == k else x[2])
                               for j, x in enumerate(g))
                lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(end)},Cap,,0,0,0,,{txt}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")


# ---------- face layouts ----------
def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        print("Installing OpenCV for face tracking (one time)...", flush=True)
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "opencv-python-headless"], capture_output=True)
        try:
            import cv2
            return cv2
        except ImportError:
            return None


def face_plan(video, dur, samples=14):
    """Looks for a face across the clip. Returns:
      ("cam", (x, y, w, h), W, H)          a small, steady face = a facecam: put it on top, gameplay below
      ("track", [(t, cx), ...], W, H)      a big face (in-person video): keep the vertical crop centered on it
      None                                 no reliable face"""
    cv2 = _cv2()
    if not cv2:
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(video)
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not W or not H or H > W:  # already vertical: nothing to do
        cap.release()
        return None
    found = []
    for t in [dur * (k + 0.5) / samples for k in range(samples)]:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            continue
        sc = 640 / W
        g = cv2.cvtColor(cv2.resize(fr, (640, int(H * sc))), cv2.COLOR_BGR2GRAY)
        fs = casc.detectMultiScale(g, scaleFactor=1.1, minNeighbors=6, minSize=(22, 22))
        if len(fs):
            x, y, w, h = max(fs, key=lambda f: f[2] * f[3])
            found.append((t, x / sc, y / sc, w / sc, h / sc))
    cap.release()
    if len(found) < samples * 0.45:
        return None
    import statistics as st
    mw = st.median(f[3] for f in found)
    cxs = [f[1] + f[3] / 2 for f in found]
    if mw < 0.16 * W and (max(cxs) - min(cxs)) < 0.12 * W:  # small and not moving = facecam overlay
        x, y, h = st.median(f[1] for f in found), st.median(f[2] for f in found), st.median(f[4] for f in found)
        cw = min(W, mw * 3.2)
        ch = min(H, cw * 640 / 1080)
        cx, cy = x + mw / 2, y + h / 2
        return ("cam", (int(max(0, min(W - cw, cx - cw / 2))), int(max(0, min(H - ch, cy - ch / 2))), int(cw), int(ch)), W, H)
    return ("track", [(f[0], f[1] + f[3] / 2) for f in found], W, H)


def face_filter(plan):
    """ffmpeg filter (input [0:v] -> 1080x1920) for a face plan."""
    kind, data, W, H = plan
    if kind == "cam":
        x, y, w, h = data
        gw = min(W, int(H * 1080 / 1280) // 2 * 2)
        # gameplay: the part of the screen away from the facecam
        gx = 0 if x + w / 2 > W / 2 else W - gw
        gx = int(max(0, min(W - gw, (W - gw) / 2 if abs(x + w / 2 - W / 2) < W * 0.15 else gx)))
        return (f"split[c][g];[c]crop={w}:{h}:{x}:{y},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640[cam];"
                f"[g]crop={gw}:{H}:{gx}:0,scale=1080:1280:force_original_aspect_ratio=increase,crop=1080:1280[game];"
                "[cam][game]vstack")
    cw = int(H * 9 / 16) // 2 * 2
    pts = [(t, max(0, min(W - cw, cx - cw / 2))) for t, cx in data]
    # smooth, piecewise-linear camera path through the face positions
    expr = f"{pts[-1][1]:.0f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(pts, pts[1:]))):
        expr = f"if(lt(t,{t1:.2f}),{x0:.0f}+({x1 - x0:.0f})*(t-{t0:.2f})/{max(t1 - t0, 0.01):.2f},{expr})"
    expr = f"if(lt(t,{pts[0][0]:.2f}),{pts[0][1]:.0f},{expr})"
    return f"crop={cw}:{H}:'{expr}':0,scale=1080:1920"


def clip_words(audio_path, fallback_text, duration):
    """Word timings for captions: local Whisper on just the clip; evenly spaced fallback."""
    try:
        segs = transcribe(audio_path, "base", word_timestamps=True, vad_filter=True)
        words = [(w.start, w.end, w.word) for s in segs for w in (s.words or [])]
        if words:
            return words
    except Exception as ex:
        print(f"[whisper unavailable for captions: {type(ex).__name__}]")
    toks = fallback_text.split()
    if not toks:
        return []
    per = duration / len(toks)
    return [(i * per, (i + 1) * per, t) for i, t in enumerate(toks)]


def render(con, moment_id, variant):
    """Cut + edit one moment into a 1080x1920 mp4. Returns the clip row id."""
    import numpy as np

    vid, start, end, text, hook, title = con.execute(
        "SELECT video_id, start, end, text, hook, title FROM mm_moments WHERE id=?", (moment_id,)).fetchone()
    import mm_words  # the on-screen hook uses platform-safe wording (TikTok reads on-screen text too)
    hook = mm_words.clean(hook or "", kind="title")
    src = con.execute("SELECT path FROM mm_sources WHERE video_id=?", (vid,)).fetchone()[0]
    if variant.get("length") == "short":
        end = min(end, start + random.uniform(20, 30))
    dur = end - start
    work = os.path.join(OUT, f"m{moment_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    os.makedirs(work, exist_ok=True)
    ff = ffmpeg_exe()
    # cut the raw piece first (fast seek), audio as wav for analysis/captions
    run([ff, "-y", "-ss", f"{start:.2f}", "-i", os.path.abspath(src), "-t", f"{dur:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "raw.mp4"], cwd=work)
    run([ff, "-y", "-i", "raw.mp4", "-vn", "-ac", "1", "-ar", "16000", "audio.wav"], cwd=work)

    # base 9:16 layout
    plan = None
    if variant.get("layout") == "face":
        try:
            plan = face_plan(os.path.join(work, "raw.mp4"), dur)
        except Exception as e:
            print(f"[face tracking skipped: {e}]")
        variant["layout_used"] = plan[0] if plan else "blur"
    if plan:
        base = face_filter(plan)
        print(f"Layout: {'facecam on top + gameplay below' if plan[0] == 'cam' else 'following the face'}")
    elif variant.get("layout") == "crop":
        base = "scale=-2:1920,crop=1080:1920"
    else:
        base = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "boxblur=20:5[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    graph = f"[0:v]{base},fps=30,setsar=1[base]"
    last = "base"

    # punch-in zooms on the loudest moments (segments rendered zoomed, then concatenated)
    if variant.get("zoom") == "punch":
        with wave.open(os.path.join(work, "audio.wav"), "rb") as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        step = 0.25
        n = int(16000 * step)
        frames = max(len(data) // n, 1)
        rms = 20 * np.log10(np.sqrt((data[: frames * n].reshape(frames, n) ** 2).mean(axis=1) + 1e-9) + 1e-9)
        thr = np.percentile(rms, 92)
        spans = []
        for i in np.where(rms >= thr)[0]:
            t = i * step
            if spans and t - spans[-1][1] < 0.6:
                spans[-1][1] = t + 0.8
            else:
                spans.append([t, t + 0.8])
        spans = [s for s in spans if s[1] - s[0] >= 0.5][:6]
        if spans:
            cuts, cursor, parts = [], 0.0, []
            for s, e in spans:
                if s > cursor:
                    cuts.append((cursor, s, False))
                cuts.append((s, min(e, dur), True))
                cursor = min(e, dur)
            if cursor < dur:
                cuts.append((cursor, dur, False))
            graph += f";[base]split={len(cuts)}" + "".join(f"[s{i}]" for i in range(len(cuts)))
            for i, (s, e, z) in enumerate(cuts):
                zoom = ",crop=iw/1.22:ih/1.22,scale=1080:1920,setsar=1" if z else ""
                graph += f";[s{i}]trim=start={s:.2f}:end={e:.2f},setpts=PTS-STARTPTS{zoom},setsar=1[z{i}]"
                parts.append(f"[z{i}]")
            graph += ";" + "".join(parts) + f"concat=n={len(cuts)}:v=1:a=0[zoomed]"
            last = "zoomed"

    # captions + hook text (ASS subtitles, needs libass)
    want_caps = variant.get("captions") in ("word", "karaoke", "single")
    want_hook = variant.get("hook") == "text" and hook
    if (want_caps or want_hook) and has_filter("subtitles"):
        words = clip_words(os.path.join(work, "audio.wav"), text, dur) if want_caps else []
        write_ass(os.path.join(work, "caps.ass"), words, hook if want_hook else None,
                  style="single" if variant.get("captions") == "single" else "karaoke",
                  margin_v=700 if plan and plan[0] == "cam" else 520)
        graph += f";[{last}]subtitles=caps.ass[final]"
        last = "final"
    elif want_caps or want_hook:
        print("[ffmpeg has no 'subtitles' filter here; rendering without captions]")

    run([ff, "-y", "-i", "raw.mp4", "-filter_complex", graph, "-map", f"[{last}]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", "final.mp4"], cwd=work)
    final = os.path.join(work, "final.mp4")
    if variant.get("music") == "bed":  # the bot's own AI music under the clip
        try:
            import mm_audio
            mtype = con.execute("SELECT COALESCE(mtype,'funny') FROM mm_moments WHERE id=?", (moment_id,)).fetchone()[0]
            track, mood = mm_audio.pick(con, mm_audio.TYPE_MOOD.get(mtype), mm_safety.config().get("ollama_model"))
            if track:
                bed = os.path.join(work, "final_music.mp4")
                mm_audio.add_bed(final, track, bed)
                os.replace(bed, final)
                variant["music_mood"] = mood
            else:
                variant["music"] = "none"
        except Exception as e:
            print(f"[music bed skipped: {e}]")
            variant["music"] = "none"
    for tmp in ("raw.mp4", "audio.wav"):
        try:
            os.remove(os.path.join(work, tmp))
        except OSError:
            pass
    mark_used(con, moment_id)
    cur = con.execute("INSERT INTO mm_clips (moment_id, path, variant, hook, title, created_at) VALUES (?,?,?,?,?,?)",
                      (moment_id, final, json.dumps(variant), hook, title, datetime.now(timezone.utc).isoformat()))
    con.commit()
    return cur.lastrowid


# Best unused moment, but from the video that has had the FEWEST clips made so far (variety across the channel)
ONLY_VIDEO = None  # set by "clip-from": make the next clip from this one video
SPREAD_SQL = ("SELECT m.id, COALESCE(m.mtype,'funny'), m.video_id FROM mm_moments m WHERE m.used=0 AND m.ai_score>=6 "
              "AND COALESCE((SELECT priority FROM mm_library l WHERE l.video_id=m.video_id), 0) >= ? ORDER BY "
              "COALESCE((SELECT priority FROM mm_library l WHERE l.video_id=m.video_id), 0) DESC, "
              "(SELECT COUNT(*) FROM mm_clips c JOIN mm_moments x ON x.id=c.moment_id WHERE x.video_id=m.video_id) ASC, "
              "m.ai_score DESC, m.loudness DESC LIMIT 40")


class _Pick:
    def __init__(self, con):
        self.con = con

    def fetchone(self):
        return pick_moment_row(self.con)


PAD = 8  # seconds: moments this close to an already-used one count as the same moment


def mark_used(con, moment_id):
    """Use a moment, and retire every other moment that overlaps it (the same bit found twice), so it's never
    clipped again."""
    r = con.execute("SELECT video_id, start, end FROM mm_moments WHERE id=?", (moment_id,)).fetchone()
    con.execute("UPDATE mm_moments SET used=1 WHERE id=?", (moment_id,))
    if r and r[0] not in ("ai", "upload"):
        con.execute("UPDATE mm_moments SET used=3 WHERE used=0 AND video_id=? AND start < ? AND end > ?",
                    (r[0], r[2] + PAD, r[1] - PAD))  # 3 = same moment as one already used
    con.commit()


def _overlaps_used(con, rows):
    """Drop moments that overlap anything already clipped (catches older data from before mark_used)."""
    vids = {r[2] for r in rows}
    used = {}
    for vid, s, e in con.execute("SELECT m.video_id, m.start, m.end FROM mm_moments m WHERE m.used IN (1, 3) OR "
                                 "EXISTS (SELECT 1 FROM mm_clips c WHERE c.moment_id=m.id)"):
        if vid in vids:
            used.setdefault(vid, []).append((s, e))
    out = []
    for r in rows:
        s, e = con.execute("SELECT start, end FROM mm_moments WHERE id=?", (r[0],)).fetchone()
        if any(s < ue + PAD and e > us - PAD for us, ue in used.get(r[2], [])):
            con.execute("UPDATE mm_moments SET used=3 WHERE id=?", (r[0],))
            continue
        out.append(r)
    con.commit()
    return out


def recycle_moment(con):
    """Only when there are NO good fresh moments anywhere: the best moment that was clipped longest ago
    (it gets a different style this time)."""
    return con.execute("SELECT m.id, COALESCE(m.mtype,'funny'), m.video_id FROM mm_moments m JOIN mm_clips c ON "
                       "c.moment_id=m.id WHERE m.video_id NOT IN ('ai','upload') AND m.ai_score>=7 AND c.status='ready' "
                       "GROUP BY m.id ORDER BY MAX(c.created_at) ASC, m.ai_score DESC LIMIT 1").fetchone()


def pick_moment_row(con):
    """Variety: from the least-clipped videos' best moments, the brain picks a moment TYPE (funny, clutch, fail,
    rage...) by how each type has done vs usual, and never the same video twice in a row."""
    ensure_library(con)
    if ONLY_VIDEO:
        rows = con.execute("SELECT m.id, COALESCE(m.mtype,'funny'), m.video_id FROM mm_moments m WHERE m.used=0 AND "
                           "m.video_id=? ORDER BY m.ai_score DESC LIMIT 10", (ONLY_VIDEO,)).fetchall()
        rows = _overlaps_used(con, rows)
        return rows[0] if rows else None
    picked_only = mm_safety.config().get("source_mode", "random") == "picked"
    rows = con.execute(SPREAD_SQL, (1 if picked_only else 0,)).fetchall()  # 1 = starred only; 0 = everything not blocked
    rows = _overlaps_used(con, rows)
    if not rows:
        if picked_only:
            print("No unused moments left in your starred videos. Star more in the app's Videos tab (or turn off "
                  "'Only use videos I starred').")
        return None
    recent = {r[0] for r in con.execute("SELECT m.video_id FROM mm_clips c JOIN mm_moments m ON m.id=c.moment_id "
                                        "ORDER BY c.id DESC LIMIT 3")}
    pool = [r for r in rows if r[2] not in recent] or rows  # a different video than the last 3 clips
    pool = pool[:20]
    types = list(dict.fromkeys(r[1] for r in pool))
    try:
        import mm_brain
        want = mm_brain.choose(con, "moment_type", types, log=False)
    except Exception:
        want = random.choice(types)
    return next(r for r in pool if r[1] == want)


def produce_one(con, ask):
    """Make one ready-to-post clip: download/analyze a source if needed, pick the best unused moment."""
    ensure_tables(con)
    row = _Pick(con).fetchone()
    if not row:
        todo = con.execute("SELECT video_id FROM mm_sources WHERE analyzed=0 LIMIT 1").fetchone()
        if not todo and not ONLY_VIDEO:
            if sync_sources(con) == 0:
                print("No new source videos to download.")
            todo = con.execute("SELECT video_id FROM mm_sources WHERE analyzed=0 LIMIT 1").fetchone()
        if not todo:
            print("Nothing left to analyze.")
            return None
        print(f"Finding moments in {todo[0]}...", flush=True)
        try:
            find_moments(con, todo[0], ask)
        except Exception as e:
            print(f"[analysis failed for {todo[0]}: {e}; skipping this video]")
            con.execute("UPDATE mm_sources SET analyzed=1 WHERE video_id=?", (todo[0],))
            con.commit()
        row = _Pick(con).fetchone()
    if not row and not ONLY_VIDEO:
        row = recycle_moment(con)
        if row:
            print(f"No fresh good moments left anywhere, so re-using moment {row[0]} in a new style. Get more of your "
                  "videos in (Videos tab) so it doesn't have to.")
    if not row:
        print("No moment scored 6+ yet.")
        return None
    # last check: don't re-make a bit the creator already posted as a Short (their Shorts are learned over time)
    txt = con.execute("SELECT text FROM mm_moments WHERE id=?", (row[0],)).fetchone()[0]
    if already_made(con, txt):
        con.execute("UPDATE mm_moments SET used=2 WHERE id=?", (row[0],))  # 2 = skipped as duplicate
        con.commit()
        print(f"Moment {row[0]} matches one of your existing Shorts; skipping it")
        return produce_one(con, ask)
    variant = choose_variant(con)
    import mm_voice
    if variant.get("voice") == "ai_hook" and not mm_voice.available(con):
        variant["voice"] = "none"
    con.execute("UPDATE mm_library SET priority=0 WHERE priority=1 AND video_id IN (SELECT x.video_id FROM mm_clips c "
                "JOIN mm_moments x ON x.id=c.moment_id GROUP BY x.video_id HAVING COUNT(*)>=2)")
    con.commit()
    print(f"Rendering moment {row[0]} with style {variant}", flush=True)
    cid = render(con, row[0], variant)
    if cid and variant.get("voice") == "ai_hook":
        path, hook = con.execute("SELECT path, hook FROM mm_clips WHERE id=?", (cid,)).fetchone()
        line = hook or "bro you are NOT ready for this"
        try:
            out = path.replace("final.mp4", "final_voice.mp4")
            mm_voice.add_hook(path, line, out)
            con.execute("UPDATE mm_clips SET path=?, ai_voice=1 WHERE id=?", (out, cid))
            con.commit()
            print(f"Added an AI-voice hook in your voice: \"{line}\"")
        except Exception as e:
            con.execute("UPDATE mm_clips SET variant=? WHERE id=?", (json.dumps(dict(variant, voice="none")), cid))
            con.commit()
            print(f"[AI voice hook skipped: {e}]")
    return cid
