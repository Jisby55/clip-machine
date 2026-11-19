"""
Picture factory for Instagram (all local, all from the creator's own footage or local AI).

Kinds:
  meme      one frame from the creator's own video + a meme caption (two looks: "impact" or "band")
  carousel  4-6 frames walking through one moment, a caption on each slide (first slide = hook)
  ai_image  local AI image (ComfyUI + Z-Image Turbo) of the creator lore / gaming jokes. Never real people.
            Captions always say it's AI-generated.
Output: 1080x1350 (4:5) sRGB JPEGs, which is what Instagram wants.
"""
import json
import os
import random
import re
import subprocess
import textwrap
import time
import uuid

import requests

import mm_video

OUT = os.path.join("media", "images")
W, H = 1080, 1350
FONTS = {"impact": [r"C:\Windows\Fonts\impact.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
                    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf", r"C:\Windows\Fonts\arialbd.ttf",
                    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
         "bold": [r"C:\Windows\Fonts\arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]}
COMFY_PORTS = (8000, 8188)
ZIMAGE = {"unet": "z_image_turbo_bf16.safetensors", "clip": "qwen_3_4b.safetensors", "vae": "ae.safetensors"}


# ---------- helpers ----------
def _font(kind, size):
    from PIL import ImageFont
    for p in FONTS[kind]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def _json(raw):
    try:
        return json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        return {}


def grab_frame(src_path, t):
    """One frame (PIL image) at second t of a source video."""
    from PIL import Image
    os.makedirs(OUT, exist_ok=True)  # first picture of a run used to fail because this folder didn't exist yet
    tmp = os.path.join(OUT, f"_f{uuid.uuid4().hex[:8]}.png")
    mm_video.run([mm_video.ffmpeg_exe(), "-y", "-ss", f"{t:.2f}", "-i", src_path, "-frames:v", "1", "-update", "1", tmp])
    img = Image.open(tmp).convert("RGB")
    img.load()
    os.remove(tmp)
    return img


def _fit_text(draw, text, font_kind, max_w, max_lines, start_size):
    """Largest font size where the wrapped text fits max_w and max_lines."""
    size = start_size
    while size > 28:
        f = _font(font_kind, size)
        avg = max(1, draw.textlength("abcdefghijklmnopqrstuvwxyz", font=f) / 26)
        lines = textwrap.wrap(text, width=max(8, int(max_w / avg)))
        if len(lines) <= max_lines and all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f, lines
        size -= 4
    f = _font(font_kind, 28)
    return f, textwrap.wrap(text, width=40)[:max_lines]


def style_impact(img, top, bottom=""):
    """Classic meme: frame cropped to 4:5, white Impact text with black outline top/bottom."""
    from PIL import ImageDraw
    sw, sh = img.size
    tw = int(sh * W / H)
    if tw <= sw:
        img = img.crop(((sw - tw) // 2, 0, (sw - tw) // 2 + tw, sh))
    img = img.resize((W, H))
    d = ImageDraw.Draw(img)
    for text, at_top in ((top, True), (bottom, False)):
        if not text:
            continue
        f, lines = _fit_text(d, text.upper(), "impact", W - 80, 3, 110)
        lh = f.size + 10
        y = 40 if at_top else H - 40 - lh * len(lines)
        for line in lines:
            x = (W - d.textlength(line, font=f)) / 2
            d.text((x, y), line, font=f, fill="white", stroke_width=max(3, f.size // 14), stroke_fill="black")
            y += lh
    return img


def text_overlay(text, w=1080, h=1920):
    """Transparent layer with big white Impact meme text (black outline) near the top."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if not text:
        return layer
    d = ImageDraw.Draw(layer)
    f, lines = _fit_text(d, text.upper(), "impact", w - 100, 3, 110)
    y = int(h * 0.08)
    for line in lines:
        x = (w - d.textlength(line, font=f)) / 2
        d.text((x, y), line, font=f, fill="white", stroke_width=max(3, f.size // 14), stroke_fill="black")
        y += f.size + 10
    return layer


def style_band(img, text):
    """Twitter-style meme: white band with black caption on top, the frame underneath, blurred fill."""
    from PIL import Image, ImageDraw, ImageFilter
    canvas = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(canvas)
    f, lines = _fit_text(d, text, "bold", W - 100, 5, 64)
    lh = int(f.size * 1.25)
    band = 60 + lh * len(lines)
    y = 40
    for line in lines:
        d.text((50, y), line, font=f, fill="black")
        y += lh
    area_h = H - band
    sw, sh = img.size
    bg = img.resize((W, int(sh * W / sw)) if sh * W / sw >= area_h else (int(sw * area_h / sh), area_h))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - area_h) // 2, (bg.width - W) // 2 + W, (bg.height - area_h) // 2 + area_h))
    canvas.paste(bg.filter(ImageFilter.GaussianBlur(28)), (0, band))
    fg = img.resize((W, int(sh * W / sw)))
    if fg.height > area_h:
        fg = fg.crop((0, (fg.height - area_h) // 2, W, (fg.height - area_h) // 2 + area_h))
    canvas.paste(fg, (0, band + (area_h - fg.height) // 2))
    return canvas


def save(img, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}.jpg")
    img.convert("RGB").save(path, "JPEG", quality=92)
    return path


# ---------- picking source moments ----------
def pick_moment(con, moment_id=None):
    """A rated moment from the creator's own videos that hasn't been used for a picture yet (or a specific one)."""
    try:
        con.execute("ALTER TABLE mm_moments ADD COLUMN pic_used INTEGER DEFAULT 0")
        con.commit()
    except Exception:
        pass
    if moment_id:
        m = con.execute("SELECT m.id, m.start, m.end, m.text, m.hook, s.path, s.title FROM mm_moments m JOIN mm_sources s "
                        "ON s.video_id=m.video_id WHERE m.id=?", (moment_id,)).fetchone()
        if m and m[5] and os.path.exists(m[5]):
            return m
    row = con.execute(
        "SELECT m.id, m.start, m.end, m.text, m.hook, s.path, s.title FROM mm_moments m JOIN mm_sources s "
        "ON s.video_id=m.video_id WHERE COALESCE(m.pic_used,0)=0 AND m.text IS NOT NULL ORDER BY m.ai_score DESC LIMIT 40"
    ).fetchall()
    if not row:
        return None
    # spread pictures across different videos: pick a random video first, then its best moment
    vids = {}
    for r in row:
        vids.setdefault(r[5], []).append(r)
    m = random.choice(list(vids.values()))[0]
    if not os.path.exists(m[5]):
        con.execute("UPDATE mm_moments SET pic_used=1 WHERE id=?", (m[0],))
        con.commit()
        return pick_moment(con)
    return m


# ---------- kinds ----------
def make_meme(con, ask, identity, memory, moment_id=None):
    m = pick_moment(con, moment_id)
    if not m:
        return None
    mid, start, end, text, hook, src, title = m
    data = _json(ask(
        f"{identity}\n\nYOUR MEMORY:\n{memory}\n\nYou're making an Instagram meme from a frame of your video "
        f"\"{title}\". What's said around it: {text[:700]}\n\n"
        "Write a meme caption that's funny without context (relatable gamer pain, absurd, dumb). Rules: no real "
        "tragedies, no slurs, nothing about real people's looks. Reply ONLY JSON: {\"top\": \"short top text\", "
        "\"bottom\": \"short punchline or empty\", \"band\": \"one-line tweet-style caption\", \"post\": \"1-2 sentence "
        "Instagram caption in your voice that makes people comment\"}", temperature=0.9))
    import mm_words  # text burned into the picture gets the same platform-safe wording
    for k in ("top", "bottom", "band"):
        if isinstance(data.get(k), str):
            data[k] = mm_words.clean(data[k], kind="title")
    if not data.get("top") and not data.get("band"):
        return None
    look = random.choice(["impact", "band"])
    frame = grab_frame(src, start + (end - start) * random.uniform(0.3, 0.7))
    img = style_impact(frame, data.get("top", ""), data.get("bottom", "")) if look == "impact" \
        else style_band(frame, data.get("band") or data.get("top"))
    con.execute("UPDATE mm_moments SET pic_used=1 WHERE id=?", (mid,))
    con.commit()
    return {"kind": "meme", "style": look, "paths": [save(img, "meme")], "caption": data.get("post", ""),
            "about": f"{title}: {text[:300]}", "ai": False,
            "onimage": " / ".join(x for x in (data.get("top"), data.get("bottom"), data.get("band")) if x)}


def make_carousel(con, ask, identity, memory, moment_id=None):
    m = pick_moment(con, moment_id)
    if not m:
        return None
    mid, start, end, text, hook, src, title = m
    n = random.randint(4, 6)
    data = _json(ask(
        f"{identity}\n\nYOUR MEMORY:\n{memory}\n\nYou're making a {n}-slide Instagram carousel that tells the story of "
        f"this moment from your video \"{title}\": {text[:900]}\n\nSlide 1 is a hook that makes people swipe; the last "
        "slide is the punchline. Short, dumb, funny. Reply ONLY JSON: {\"slides\": [\"slide 1 text\", ...], "
        "\"post\": \"1-2 sentence caption in your voice that makes people comment\"}", temperature=0.85))
    import mm_words
    slides = [mm_words.clean(s, kind="title") for s in (data.get("slides") or []) if isinstance(s, str) and s.strip()]
    slides = [s for s in slides if s][:n]
    if len(slides) < 3:
        return None
    span = max(end - start, 3)
    paths = []
    for i, s in enumerate(slides):
        frame = grab_frame(src, start + span * (i + 0.5) / len(slides))
        paths.append(save(style_band(frame, s), f"car{i + 1}"))
    con.execute("UPDATE mm_moments SET pic_used=1 WHERE id=?", (mid,))
    con.commit()
    return {"kind": "carousel", "style": f"{len(paths)}slides", "paths": paths, "caption": data.get("post", ""),
            "about": f"{title}: {text[:300]}", "ai": False, "onimage": " / ".join(slides)}


# ---------- local AI images (ComfyUI) ----------
_STARTED = {"proc": None}


def comfy_exe():
    """Where ComfyUI Desktop is installed (machine_config "comfy_exe" wins, else the usual install folders)."""
    import glob
    try:
        import mm_safety
        p = mm_safety.config().get("comfy_exe")
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    import sys as _sys
    if _sys.platform == "darwin":
        for c in ("/Applications/ComfyUI.app/Contents/MacOS/ComfyUI", os.path.expanduser("~/Applications/ComfyUI.app/Contents/MacOS/ComfyUI")):
            if os.path.exists(c):
                return c
        return None
    la = os.environ.get("LOCALAPPDATA", "")
    for pat in (r"Programs\@comfyorgcomfyui-electron\ComfyUI.exe", r"Programs\ComfyUI\ComfyUI.exe",
                r"Programs\*omfy*\ComfyUI.exe", r"Programs\*omfy*\*.exe"):
        hits = [h for h in glob.glob(os.path.join(la, pat)) if "uninstall" not in h.lower()]
        if hits:
            return hits[0]
    return None


def comfy_available():
    """ComfyUI is open (or the bot can open it) AND heavy AI is allowed right now (no games for 90 minutes)."""
    import mm_gpu
    return mm_gpu.heavy_ok()[0] and bool(comfy_url() or comfy_exe())


def ensure_comfy(wait=240):
    """Open ComfyUI Desktop if it isn't running (the bot closes it again at the end of the run). Returns the URL."""
    import mm_gpu
    ok, why = mm_gpu.heavy_ok()
    if not ok:
        print(f"AI art/video/music is waiting: {why}")
        return None
    url = comfy_url()
    if url:
        return url
    exe = comfy_exe()
    if not exe:
        print("ComfyUI isn't open and I can't find ComfyUI Desktop (set \"comfy_exe\" in machine_config.json)")
        return None
    print("Opening ComfyUI Desktop for AI images/videos (closes again after this run)...", flush=True)
    _STARTED["proc"] = subprocess.Popen([exe], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    import atexit
    atexit.register(stop_comfy_if_started)  # also closes it if a manual command (Post now, Full AI) opened it
    t = time.time()
    while time.time() - t < wait:
        time.sleep(5)
        url = comfy_url()
        if url:
            time.sleep(5)  # let it finish loading its nodes
            print(f"ComfyUI is up after {time.time() - t:.0f}s")
            return url
    print("ComfyUI didn't start in time; skipping AI art this run")
    return None


def stop_comfy_if_started():
    """Close ComfyUI only if the bot opened it (never closes one you opened yourself)."""
    p = _STARTED["proc"]
    if not p:
        return
    _STARTED["proc"] = None
    try:
        import sys as _sys
        if _sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            p.terminate()
        print("Closed ComfyUI (the bot had opened it)")
    except Exception as e:
        print(f"[couldn't close ComfyUI: {e}]")


def comfy_url():
    for port in COMFY_PORTS:
        try:
            requests.get(f"http://127.0.0.1:{port}/system_stats", timeout=3)
            return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return None


def comfy_generate(prompt, seed=None, width=960, height=1200):
    """Z-Image Turbo text-to-image through ComfyUI's API. Returns a PIL image, then frees VRAM."""
    from io import BytesIO
    from PIL import Image
    base = comfy_url()
    if not base:
        raise RuntimeError("ComfyUI isn't running (open ComfyUI Desktop and leave it open for AI images)")
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": ZIMAGE["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": ZIMAGE["clip"], "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": ZIMAGE["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0],
                                                    "latent_image": ["6", 0], "seed": seed or random.randint(1, 2**31),
                                                    "steps": 8, "cfg": 1.0, "sampler_name": "res_multistep",
                                                    "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "clip_machine"}},
    }
    r = requests.post(f"{base}/prompt", json={"prompt": wf, "client_id": "clip-machine"}, timeout=30).json()
    if "prompt_id" not in r:
        raise RuntimeError(f"ComfyUI rejected the workflow: {str(r)[:400]}")
    pid = r["prompt_id"]
    try:
        for _ in range(240):  # up to 8 min (first load of the model is the slow part)
            time.sleep(2)
            h = requests.get(f"{base}/history/{pid}", timeout=10).json().get(pid)
            if h and h.get("outputs", {}).get("10"):
                im = h["outputs"]["10"]["images"][0]
                raw = requests.get(f"{base}/view", params={"filename": im["filename"], "subfolder": im["subfolder"],
                                                          "type": im["type"]}, timeout=30).content
                return Image.open(BytesIO(raw)).convert("RGB")
            if h and h.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"ComfyUI error: {str(h['status'])[:400]}")
        raise RuntimeError("ComfyUI took too long")
    finally:
        try:  # give the VRAM back (so games and Ollama aren't starved)
            requests.post(f"{base}/free", json={"unload_models": True, "free_memory": True}, timeout=10)
        except Exception:
            pass


def unload_ollama(model):
    try:
        requests.post("http://localhost:11434/api/generate", json={"model": model, "keep_alive": 0}, timeout=20)
    except Exception:
        pass


def make_ai_image(con, ask, identity, memory, ollama_model=None):
    if not ensure_comfy():
        return None
    data = _json(ask(
        f"{identity}\n\nYOUR MEMORY:\n{memory}\n\nMake an Instagram AI image post. Pick something from your lore "
        "([LORE], your streams) or a dumb gaming joke. Write an image prompt for an "
        "image model: detailed, visual, funny, vivid colors, cartoon/3D game render style. HARD RULES for the image: "
        "no real people, no celebrities, no real brands/logos, no copyrighted characters, nothing gory or sexual. "
        "Reply ONLY JSON: {\"prompt\": \"...\", \"text\": \"optional short meme text to put on it or empty\", "
        "\"post\": \"1-2 sentence caption in your voice that makes people comment\"}", temperature=0.95))
    if not data.get("prompt"):
        return None
    if ollama_model:
        unload_ollama(ollama_model)  # free VRAM for the image model
    img = comfy_generate(data["prompt"]).resize((W, H))
    if data.get("text"):
        img = style_impact(img, data["text"])
    return {"kind": "ai_image", "style": "zimage" + ("_text" if data.get("text") else ""),
            "paths": [save(img, "ai")], "caption": data.get("post", ""), "about": data["prompt"][:300], "ai": True,
            "onimage": f"{data.get('text', '')} (image of: {data['prompt'][:300]})"}


KINDS = {"meme": make_meme, "carousel": make_carousel, "ai_image": make_ai_image}
