"""
Custom content: upload a picture or video and/or type what you want, and the machine makes it (nothing gets posted).

  picture + text   your edits. Classic edits (crop, rotate, looks, meme text...) always work. Edits that change
                   what's IN the picture ("remove the hat", "make it a cartoon") need ComfyUI + Qwen-Image-Edit.
  picture alone    the AI picks the edits (it looks at the picture if you have an Ollama vision model)
  video + text     your edits: trim, speed, reverse, vertical, zoom, looks, captions, big text, music. "Turn it
                   into an anime clip"-style requests become an AI restyle (ComfyUI + Wan 2.2) when installed.
  video alone      the AI picks the edits (from what's said in it, and what it sees with a vision model)
  text alone       the AI decides: an AI picture or a short fully-AI video about it
  nothing          something random in your channel's style

Everything works out of the box without ComfyUI: AI-only requests fall back to classic edits (or a text graphic)
and say what to install. Results go to media/custom/ and open when they're done.
"""
import base64
import io
import json
import os
import random
import re
import shutil
import time
import uuid

import requests

import mm_images
import mm_video

OUT = os.path.join("media", "custom")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
LOOKS = ("none", "bw", "vintage", "vivid", "warm", "cool", "dark", "bright", "dramatic")
CROPS = {"4:5": (4, 5), "9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}
MOODS = ("hype", "funny", "chill", "dramatic", "spooky", "retro")
# Default ComfyUI model files (override any of them in machine_config.json). Matching is forgiving: any installed
# file whose name contains the key words below counts.
EDIT_DEFAULTS = {"image_edit_model": "qwen_image_edit_2509_fp8_e4m3fn.safetensors",
                 "image_edit_clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
                 "image_edit_vae": "qwen_image_vae.safetensors",
                 "image_edit_lora": "Qwen-Image-Lightning-4steps-V1.0.safetensors"}
EDIT_MATCH = {"image_edit_model": ("qwen", "image", "edit"), "image_edit_clip": ("qwen", "2.5", "vl"),
              "image_edit_vae": ("qwen", "image", "vae"), "image_edit_lora": ("qwen", "image", "lightning")}
INSTALL_EDIT = ("For AI picture edits, install ComfyUI Desktop and put the Qwen-Image-Edit files in its models folder "
                "(see 'Custom content' in the README).")
INSTALL_VIDEO = ("For AI video restyles, install ComfyUI Desktop and the Wan 2.2 image-to-video files "
                 "(see 'Custom content' in the README).")
RULES = ("HARD RULES for anything AI-generated: no real people, no celebrities, no real brands/logos, no copyrighted "
         "characters, nothing gory or sexual.")


def _cfg():
    try:
        import mm_safety
        return mm_safety.config()
    except Exception:
        return {}


def _out(name, ext):
    os.makedirs(OUT, exist_ok=True)
    return os.path.join(OUT, f"{name}_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}{ext}")


def _num(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


# ======================= what's installed =======================

def _choices(base, node, field):
    """The file names ComfyUI offers for one loader input ([] if the node isn't there)."""
    try:
        info = requests.get(f"{base}/object_info/{node}", timeout=15).json().get(node)
        spec = info["input"]["required"][field]
        opts = spec[0]
        if isinstance(opts, str) and len(spec) > 1 and isinstance(spec[1], dict):  # newer "COMBO" format
            opts = spec[1].get("options", [])
        return list(opts) if isinstance(opts, list) else []
    except Exception:
        return []


def _has_node(base, node):
    try:
        return bool(requests.get(f"{base}/object_info/{node}", timeout=15).json().get(node))
    except Exception:
        return False


def _find(files, want, words):
    if want in files:
        return want
    for f in files:
        low = f.lower().replace("-", "_")
        if all(w in low for w in words):
            return f
    return None


def edit_models(base):
    """{'unet','clip','vae','lora'(maybe None),'node'} when Qwen-Image-Edit is installed in ComfyUI, else None."""
    if not base:
        return None
    cfg = _cfg()
    want = {k: cfg.get(k) or v for k, v in EDIT_DEFAULTS.items()}
    unet = _find(_choices(base, "UNETLoader", "unet_name"), want["image_edit_model"], EDIT_MATCH["image_edit_model"])
    clip = _find(_choices(base, "CLIPLoader", "clip_name"), want["image_edit_clip"], EDIT_MATCH["image_edit_clip"])
    vae = _find(_choices(base, "VAELoader", "vae_name"), want["image_edit_vae"], EDIT_MATCH["image_edit_vae"])
    node = next((n for n in ("TextEncodeQwenImageEditPlus", "TextEncodeQwenImageEdit") if _has_node(base, n)), None)
    if not (unet and clip and vae and node):
        return None
    lora = _find(_choices(base, "LoraLoaderModelOnly", "lora_name"), want["image_edit_lora"],
                 EDIT_MATCH["image_edit_lora"])
    return {"unet": unet, "clip": clip, "vae": vae, "lora": lora, "node": node, "cfgnorm": _has_node(base, "CFGNorm")}


def video_model_ok(base):
    if not base:
        return False
    import mm_aivideo
    return (mm_aivideo.WAN["high"] in _choices(base, "UNETLoader", "unet_name")
            and _has_node(base, "WanImageToVideo"))


def zimage_ok(base):
    return bool(base) and mm_images.ZIMAGE["unet"] in _choices(base, "UNETLoader", "unet_name")


def comfy(need=True):
    """ComfyUI's URL, opening ComfyUI Desktop if it's installed. None (quietly) when it isn't installed."""
    if not need:
        return None
    if not (mm_images.comfy_url() or mm_images.comfy_exe()):
        return None
    return mm_images.ensure_comfy()


# ======================= seeing pictures (optional Ollama vision model) =======================

def vision_model():
    """An Ollama model that can look at pictures: machine_config "vision_model", else any installed one with vision."""
    want = _cfg().get("vision_model")
    if want:
        return want
    try:
        names = [m["name"] for m in requests.get("http://localhost:11434/api/tags", timeout=5).json().get("models", [])]
    except Exception:
        return None
    for n in names:
        try:
            caps = requests.post("http://localhost:11434/api/show", json={"model": n}, timeout=10).json()
            if "vision" in (caps.get("capabilities") or []):
                return n
        except Exception:
            continue
    return None


def describe(images):
    """A few sentences about what's in these pictures (PIL images), or "" without a vision model."""
    model = vision_model()
    if not model or not images:
        return ""
    enc = []
    for im in images[:3]:
        im = im.copy()
        im.thumbnail((768, 768))
        b = io.BytesIO()
        im.convert("RGB").save(b, "JPEG", quality=85)
        enc.append(base64.b64encode(b.getvalue()).decode())
    print(f"Looking at it with {model}...", flush=True)
    try:
        r = requests.post("http://localhost:11434/api/generate", timeout=300, json={
            "model": model, "stream": False, "think": False, "images": enc, "options": {"temperature": 0.2},
            "prompt": ("Describe what's in " + ("these frames from one video" if len(enc) > 1 else "this picture")
                       + " in 2-4 plain sentences: the subject, what's happening, the setting, the mood, and any "
                         "text on it. No guessing names of real people.")}).json()
        return (r.get("response") or "").strip()
    except Exception as e:
        print(f"[couldn't look at it ({type(e).__name__}); deciding without seeing it]")
        return ""


# ======================= ComfyUI runs =======================

def _run_comfy(base, wf, out_node, minutes=15):
    from PIL import Image
    r = requests.post(f"{base}/prompt", json={"prompt": wf, "client_id": "clip-machine"}, timeout=60).json()
    if "prompt_id" not in r:
        raise RuntimeError(f"ComfyUI rejected the workflow: {str(r)[:400]}")
    pid = r["prompt_id"]
    for _ in range(int(minutes * 30)):
        time.sleep(2)
        h = requests.get(f"{base}/history/{pid}", timeout=20).json().get(pid)
        if h and h.get("outputs", {}).get(out_node):
            out = []
            for im in h["outputs"][out_node]["images"]:
                raw = requests.get(f"{base}/view", params={"filename": im["filename"], "subfolder": im["subfolder"],
                                                          "type": im["type"]}, timeout=60).content
                out.append(Image.open(io.BytesIO(raw)).convert("RGB"))
            return out
        if h and h.get("status", {}).get("status_str") == "error":
            raise RuntimeError(f"ComfyUI error: {str(h['status'])[:400]}")
    raise RuntimeError(f"ComfyUI took too long ({minutes} min)")


def ai_edit(base, models, img, instruction, seed=None):
    """Qwen-Image-Edit: picture + plain-English instruction -> edited picture."""
    import mm_aivideo
    tmp = _out("_edit_in", ".png")
    img.save(tmp)
    try:
        name = mm_aivideo._upload_image(base, tmp)
    finally:
        os.remove(tmp)
    plus = models["node"].endswith("Plus")

    def enc(text):
        ins = {"clip": ["2", 0], "prompt": text, "vae": ["3", 0]}
        ins["image1" if plus else "image"] = ["5", 0]
        return {"class_type": models["node"], "inputs": ins}
    fast = bool(models.get("lora"))
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": models["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": models["clip"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": models["vae"]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": name}},
        "5": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["4", 0], "upscale_method": "lanczos",
                                                                  "megapixels": 1.0}},
        "6": enc(instruction),
        "7": enc(""),
        "8": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
    }
    model = ["1", 0]
    if fast:
        wf["9"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": model, "lora_name": models["lora"],
                                                                   "strength_model": 1.0}}
        model = ["9", 0]
    wf["10"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": model, "shift": 3}}
    model = ["10", 0]
    if models.get("cfgnorm"):
        wf["11"] = {"class_type": "CFGNorm", "inputs": {"model": model, "strength": 1.0}}
        model = ["11", 0]
    wf["12"] = {"class_type": "KSampler", "inputs": {
        "model": model, "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["8", 0],
        "seed": seed or random.randint(1, 2**31), "steps": 4 if fast else 20, "cfg": 1.0 if fast else 2.5,
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    wf["13"] = {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["3", 0]}}
    wf["14"] = {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": "clip_machine_edit"}}
    print(f"AI edit (Qwen-Image-Edit{', fast' if fast else ''}): {instruction}", flush=True)
    try:
        return _run_comfy(base, wf, "14")[0]
    finally:
        try:
            requests.post(f"{base}/free", json={"unload_models": True, "free_memory": True}, timeout=10)
        except Exception:
            pass


# ======================= classic picture edits (Pillow, always available) =======================

def _crop(img, ratio):
    if ratio not in CROPS:
        return img
    a, b = CROPS[ratio]
    w, h = img.size
    if w / h > a / b:
        nw = int(h * a / b)
        return img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    nh = int(w * b / a)
    return img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))


def _look(img, look):
    from PIL import ImageEnhance, ImageOps
    if look == "bw":
        return ImageOps.grayscale(img).convert("RGB")
    if look == "vintage":
        g = ImageOps.grayscale(img)
        return ImageEnhance.Contrast(ImageOps.colorize(g, "#2b1d0e", "#f3e3c3", mid="#a9825a")).enhance(0.9)
    if look == "vivid":
        return ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.45)).enhance(1.12)
    if look in ("warm", "cool"):
        r, g, b = img.split()
        k = 1.12 if look == "warm" else 0.9
        r = r.point(lambda v: min(255, int(v * k)))
        b = b.point(lambda v: min(255, int(v / k)))
        from PIL import Image
        return Image.merge("RGB", (r, g, b))
    if look == "dark":
        return ImageEnhance.Contrast(ImageEnhance.Brightness(img).enhance(0.75)).enhance(1.15)
    if look == "bright":
        return ImageEnhance.Brightness(img).enhance(1.2)
    if look == "dramatic":
        return ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(0.8)).enhance(1.35)
    return img


def _meme_text(img, top, bottom):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    w, h = img.size
    for text, at_top in ((top, True), (bottom, False)):
        if not text:
            continue
        f, lines = mm_images._fit_text(d, text.upper(), "impact", w - int(w * 0.08), 3, max(40, int(w * 0.1)))
        lh = f.size + 10
        y = int(h * 0.03) if at_top else h - int(h * 0.03) - lh * len(lines)
        for line in lines:
            d.text(((w - d.textlength(line, font=f)) / 2, y), line, font=f, fill="white",
                   stroke_width=max(3, f.size // 14), stroke_fill="black")
            y += lh
    return img


def classic_image(img, p):
    from PIL import ImageEnhance, ImageFilter, ImageOps
    rot = int(_num(p.get("rotate"), 0, 270, 0)) // 90 * 90
    if rot:
        img = img.rotate(-rot, expand=True)
    if p.get("flip") is True:
        img = ImageOps.mirror(img)
    img = _crop(img, str(p.get("crop", "none")))
    img = _look(img, str(p.get("look", "none")).lower())
    for key, enh in (("brightness", ImageEnhance.Brightness), ("contrast", ImageEnhance.Contrast),
                     ("saturation", ImageEnhance.Color)):
        v = _num(p.get(key), 0.3, 2.0, 1.0)
        if abs(v - 1.0) > 0.01:
            img = enh(img).enhance(v)
    blur = _num(p.get("blur"), 0, 10, 0)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if p.get("sharpen") is True:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=3))
    if p.get("top_text") or p.get("bottom_text"):
        img = _meme_text(img, str(p.get("top_text") or ""), str(p.get("bottom_text") or ""))
    return img


# ======================= pictures =======================

IMAGE_TOOLS = """Tools (use only what's needed):
- ai_edit: ONE clear English instruction for an AI image editor, for anything that changes WHAT is in the picture
  (add/remove/replace things, new background, change the art style, outfit, weather, time of day, expressions).
  "" if not needed. {avail}
- crop: "none", "4:5" (Instagram), "9:16" (Stories/TikTok), "1:1", "16:9"
- rotate: 0, 90, 180 or 270 (clockwise); flip: true mirrors it left-right
- look: one of {looks}
- brightness, contrast, saturation: 1.0 = unchanged (0.5 to 1.8)
- sharpen: true/false; blur: 0 to 10
- top_text, bottom_text: big meme text on the picture, or ""
- caption: a 1-2 sentence post caption in your voice"""


def make_image(con, ask, identity, path, text):
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    except Exception as e:
        raise RuntimeError(f"Couldn't open that picture ({e}). Try a JPG or PNG.")
    seen = describe([img])
    base = mm_images.comfy_url()  # don't open ComfyUI just to plan; checked again if an AI edit is wanted
    models = edit_models(base) if base else None
    installed = models is not None or bool(mm_images.comfy_exe())
    avail = ("" if installed else "(The AI editor is NOT installed on this computer. Still fill ai_edit if the request "
                                  "needs it; it'll fall back to the other tools.)")
    if text:
        task = (f"Do exactly what they asked: \"{text}\". Don't add edits they didn't ask for, except a sensible "
                "crop if they mention a platform.")
    elif seen:
        task = ("They left the instructions empty: pick edits that make it a scroll-stopping post (a good crop, a "
                "look that suits it, and meme text only if something in it is genuinely funny).")
    else:
        task = ("They left the instructions empty and you CAN'T see the picture, so keep it safe: crop 4:5, a light "
                "boost (contrast 1.05, saturation 1.1, sharpen), no text, no ai_edit.")
    p = mm_images._json(ask(
        f"{identity}\n\nYou're editing a picture for social media.\n"
        + (f"What's in it: {seen}\n" if seen else "")
        + f"Size: {img.width}x{img.height}.\n{task}\n\n"
        + IMAGE_TOOLS.format(avail=avail, looks=", ".join(LOOKS))
        + f"\n{RULES}\nReply ONLY JSON with those keys.", temperature=0.6))
    if not p:
        p = {"crop": "4:5", "contrast": 1.05, "saturation": 1.1, "sharpen": True}
        print("[the AI's plan wasn't readable; doing a light auto-enhance]")
    print("Plan: " + json.dumps({k: v for k, v in p.items() if k != "caption"}), flush=True)
    note = ""
    instr = str(p.get("ai_edit") or "").strip()
    if instr:
        base = base or comfy()
        models = models or edit_models(base)
        if models:
            img = ai_edit(base, models, img, instr)
        else:
            note = f"Skipped the AI edit (\"{instr}\"): it isn't installed. {INSTALL_EDIT}"
            print(note)
    img = classic_image(img, p)
    out = _out("custom_pic", ".jpg")
    img.save(out, "JPEG", quality=92)
    return {"paths": [out], "caption": p.get("caption", ""), "note": note}


# ======================= videos =======================

def probe(path):
    """(width, height, duration, has_audio) as the video plays (phone rotation applied)."""
    import subprocess
    r = subprocess.run([mm_video.ffmpeg_exe(), "-i", path], capture_output=True, text=True, encoding="utf-8",
                       errors="replace").stderr
    m = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", r)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
    if re.search(r"rotat(e|ion of)\s*:?\s*-?(90|270)", r):
        w, h = h, w
    return w, h, mm_video.media_duration(path), bool(re.search(r"Stream #.*Audio:", r))


def _transcript(path, dur, has_audio, work):
    if not has_audio:
        return ""
    wav = os.path.join(work, "speech.wav")
    try:
        mm_video.run([mm_video.ffmpeg_exe(), "-y", "-i", path, "-t", str(min(dur, 600)), "-vn", "-ac", "1",
                      "-ar", "16000", wav])
        print("Listening to what's said in it (Whisper)...", flush=True)
        segs = mm_video.transcribe(wav, "base", vad_filter=True)
        return "\n".join(f"[{s.start:.1f}s] {s.text.strip()}" for s in segs)[:4000]
    except Exception as e:
        print(f"[couldn't transcribe it ({type(e).__name__}); deciding without the words]")
        return ""


VIDEO_TOOLS = """Tools (use only what's needed):
- trim: [start_seconds, end_seconds] to keep just that part, or null for all of it
- speed: 1.0 = normal (0.25 to 4)
- reverse: true plays it backwards (only for clips under 60 s)
- vertical: true makes it 9:16 for Shorts/TikTok/Reels (blurred background if it's wide)
- zoom: 1.0 = none (up to 2.0, crops into the middle)
- look: one of {looks}
- mute: true removes the original sound
- captions: true adds word-by-word captions of what's said
- text: big hook text at the top, or ""
- music: one of {moods} to add AI background music, or "none"
- ai_restyle: true ONLY if they want the footage turned into something else entirely (a cartoon/anime/clay version,
  a new scene). It makes a NEW ~5 s AI clip starting from a frame of the video. {avail}
- restyle_prompt: how the first frame should look (if ai_restyle); motion: what moves in the AI clip
- caption: a 1-2 sentence post caption in your voice"""


def _atempo(speed):
    parts = []
    while speed > 2.0:
        parts.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        parts.append(0.5)
        speed /= 0.5
    parts.append(speed)
    return ",".join(f"atempo={s:.4f}" for s in parts)


def _vlook(look):
    return {"bw": "hue=s=0", "vintage": "curves=preset=vintage", "vivid": "eq=saturation=1.45:contrast=1.12",
            "warm": "colorbalance=rs=0.12:gs=0.03:bs=-0.12", "cool": "colorbalance=rs=-0.1:bs=0.12",
            "dark": "eq=brightness=-0.08:contrast=1.15", "bright": "eq=brightness=0.08",
            "dramatic": "eq=contrast=1.35:saturation=0.8"}.get(look)


def classic_video(con, src, p, work, w, h, dur, has_audio):
    ff = mm_video.ffmpeg_exe()
    start, end = 0.0, dur
    t = p.get("trim")
    if isinstance(t, (list, tuple)) and len(t) == 2:
        start = _num(t[0], 0, max(0, dur - 0.5), 0)
        end = _num(t[1], start + 0.5, dur, dur)
    speed = _num(p.get("speed"), 0.25, 4.0, 1.0)
    length = (end - start) / speed
    reverse = p.get("reverse") is True
    if reverse and end - start > 60:
        print("[skipped reverse: only clips under 60 s can be reversed]")
        reverse = False
    zoom = _num(p.get("zoom"), 1.0, 2.0, 1.0)
    vertical = p.get("vertical") is True
    mute = p.get("mute") is True or not has_audio

    v = []
    if abs(speed - 1) > 0.01:
        v.append(f"setpts=PTS/{speed:.4f}")
    if reverse:
        v.append("reverse")
    if zoom > 1.01:
        v.append(f"crop=trunc(iw/{zoom:.3f}/2)*2:trunc(ih/{zoom:.3f}/2)*2")
    lk = _vlook(str(p.get("look", "none")).lower())
    if lk:
        v.append(lk)
    chain = ",".join(v + ["scale=trunc(iw/2)*2:trunc(ih/2)*2"])
    if vertical:
        graph = (f"[0:v]{chain},split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                 "boxblur=24:2[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease,"
                 "scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    else:
        graph = f"[0:v]{chain},setsar=1[v]"
    amap = []
    if not mute:
        a = []
        if abs(speed - 1) > 0.01:
            a.append(_atempo(speed))
        if reverse:
            a.append("areverse")
        graph += f";[0:a]{','.join(a) if a else 'anull'}[a]"
        amap = ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    edited = os.path.join(work, "edited.mp4")
    print(f"Editing ({length:.0f} s result)...", flush=True)
    mm_video.run([ff, "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", src, "-filter_complex", graph,
                  "-map", "[v]", *amap, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart", edited])
    cur = edited

    # text on top + word captions (second pass: captions come from the edited sound)
    want_caps = p.get("captions") is True and not mute
    hook = str(p.get("text") or "").strip()
    if want_caps or hook:
        ow, oh, odur, _ = probe(cur)
        inputs, g, last = ["-i", "edited.mp4"], "", "0:v"
        if hook:
            mm_images.text_overlay(hook, ow, oh).save(os.path.join(work, "text.png"))
            inputs += ["-i", "text.png"]
            g = "[0:v][1:v]overlay=0:0[t]"
            last = "t"
        if want_caps and mm_video.has_filter("subtitles"):
            mm_video.run([ff, "-y", "-i", cur, "-vn", "-ac", "1", "-ar", "16000", os.path.join(work, "audio.wav")])
            words = mm_video.clip_words(os.path.join(work, "audio.wav"), "", odur)
            mm_video.write_ass(os.path.join(work, "caps.ass"), words, None, style="karaoke",
                               margin_v=520 if oh > ow else 160)
            g += (";" if g else "") + f"[{last}]subtitles=caps.ass[c]"
            last = "c"
        elif want_caps:
            print("[ffmpeg has no 'subtitles' filter here; no captions]")
        if g:
            mm_video.run([ff, "-y", *inputs, "-filter_complex", g, "-map", f"[{last}]", "-map", "0:a?",
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                          "-c:a", "copy", "-movflags", "+faststart", "texted.mp4"], cwd=work)
            cur = os.path.join(work, "texted.mp4")
    return _music(con, cur, p.get("music"), mute, work)


def _music(con, video, mood, silent, work):
    mood = str(mood or "none").lower()
    if mood not in MOODS:
        return video
    try:
        import mm_audio
        track, _ = mm_audio.pick(con, mood, _cfg().get("ollama_model"))
        if not track:
            print("[no AI music available (needs the music model in ComfyUI); left without music]")
            return video
        out = os.path.join(work, "music.mp4")
        if silent:  # add_bed(solo) expects an audio track to replace; give it a silent one
            tmp = os.path.join(work, "silent.mp4")
            mm_video.run([mm_video.ffmpeg_exe(), "-y", "-i", video, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                          "-map", "0:v", "-map", "1:a", "-shortest", "-c:v", "copy", "-c:a", "aac", tmp])
            video = tmp
        mm_audio.add_bed(video, track, out, solo=silent)
        return out
    except Exception as e:
        print(f"[music skipped: {e}]")
        return video


def ai_restyle(base, src, p, work, dur):
    """A frame of the video -> (optionally AI-edited into the new look) -> a ~5 s Wan 2.2 clip."""
    import mm_aivideo
    t = p.get("trim")
    at = _num(t[0], 0, dur, 0) if isinstance(t, (list, tuple)) and t else min(dur * 0.3, 3)
    frame = mm_images.grab_frame(src, at)
    look = str(p.get("restyle_prompt") or "").strip()
    models = edit_models(base)
    if look and models:
        frame = ai_edit(base, models, frame, look)
    elif look:
        print("[no AI image editor installed: animating the original frame with the new look in the prompt]")
    first = os.path.join(work, "start.png")
    frame.save(first)
    motion = str(p.get("motion") or "natural movement, the camera slowly pushes in")
    print("Animating (Wan 2.2, a few minutes)...", flush=True)
    frames = mm_aivideo.wan_animate(first, f"{look}. {motion}" if look else motion)
    sd = os.path.join(work, "frames")
    os.makedirs(sd, exist_ok=True)
    for k, fr in enumerate(frames):
        fr.save(os.path.join(sd, f"f{k:04d}.png"))
    out = os.path.join(work, "restyled.mp4")
    mm_video.run([mm_video.ffmpeg_exe(), "-y", "-framerate", str(mm_aivideo.FPS), "-i", os.path.join(sd, "f%04d.png"),
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
    return out


def make_video(con, ask, identity, path, text):
    w, h, dur, has_audio = probe(path)
    if dur <= 0:
        raise RuntimeError("Couldn't read that video. Try an MP4 or MOV.")
    work = os.path.join(OUT, f"work_{uuid.uuid4().hex[:8]}")
    os.makedirs(work, exist_ok=True)
    try:
        said = _transcript(path, dur, has_audio, work)
        seen = describe([mm_images.grab_frame(path, dur * f) for f in (0.15, 0.5, 0.85)]) if vision_model() else ""
        base = mm_images.comfy_url()
        can_restyle = video_model_ok(base) if base else bool(mm_images.comfy_exe())
        avail = "" if can_restyle else "(NOT installed here: only pick it if nothing else can do what they asked.)"
        if text:
            task = f"Do exactly what they asked: \"{text}\". Don't add edits they didn't ask for."
        else:
            task = ("They left the instructions empty: make it a strong short. Trim to the best 15-60 s if it's "
                    "longer, make it vertical if it's wide, add captions if people talk, a hook text that makes "
                    "people stay, and music only if it has no talking.")
        p = mm_images._json(ask(
            f"{identity}\n\nYou're editing a video for social media.\n"
            f"It's {dur:.1f} s long, {w}x{h}, {'with' if has_audio else 'without'} sound.\n"
            + (f"What it looks like: {seen}\n" if seen else "")
            + (f"What's said in it (with times):\n{said}\n" if said else "")
            + f"\n{task}\n\n" + VIDEO_TOOLS.format(looks=", ".join(LOOKS), moods=", ".join(MOODS), avail=avail)
            + f"\n{RULES}\nReply ONLY JSON with those keys.", temperature=0.5))
        if not p:
            p = {"vertical": w > h, "captions": has_audio}
            print("[the AI's plan wasn't readable; doing a basic vertical edit]")
        print("Plan: " + json.dumps({k: v for k, v in p.items() if k != "caption"}), flush=True)
        note = ""
        result = None
        if p.get("ai_restyle") is True:
            base = base or comfy()
            if video_model_ok(base):
                result = ai_restyle(base, path, p, work, dur)
                result = _music(con, result, p.get("music") if p.get("music") in MOODS else "hype", True, work)
            else:
                note = f"Skipped the AI restyle: it isn't installed. {INSTALL_VIDEO} Did the other edits instead."
                print(note)
        if not result:
            result = classic_video(con, path, p, work, w, h, dur, has_audio)
        out = _out("custom_vid", ".mp4")
        shutil.move(result, out)
        return {"paths": [out], "caption": p.get("caption", ""), "note": note}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            mm_images.stop_comfy_if_started()
        except Exception:
            pass


# ======================= nothing uploaded =======================

def text_card(text, sub=""):
    """A clean text graphic (works without any AI picture model)."""
    from PIL import Image, ImageDraw
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    c1, c2 = (139, 69, 255), (20, 17, 28)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c1[i] * (1 - k) + c2[i] * k) for i in range(3)))
    f, lines = mm_images._fit_text(d, text, "impact", W - 140, 6, 120)
    lh = f.size + 14
    y = (H - lh * len(lines)) // 2 - (40 if sub else 0)
    for line in lines:
        d.text(((W - d.textlength(line, font=f)) / 2, y), line, font=f, fill="white",
               stroke_width=max(2, f.size // 18), stroke_fill="black")
        y += lh
    if sub:
        f2, l2 = mm_images._fit_text(d, sub, "bold", W - 200, 3, 48)
        y += 30
        for line in l2:
            d.text(((W - d.textlength(line, font=f2)) / 2, y), line, font=f2, fill=(236, 232, 246))
            y += int(f2.size * 1.3)
    return img


def make_from_text(con, ask, identity, memory, text):
    base = comfy()
    pic = zimage_ok(base)
    vid = pic and video_model_ok(base)
    if text:
        about = f"The user wants: \"{text}\". Make exactly that."
    else:
        about = ("The user left it blank: surprise them with something random and original that fits your channel "
                 "(your lore and running jokes, or something trending in gaming/memes).")
    options = ('"image" (an AI picture) or "video" (a short fully-AI video, ~10 min to make)' if vid
               else '"image" only')
    d = mm_images._json(ask(
        f"{identity}\n\nYOUR MEMORY:\n{memory}\n\n{about}\n\nYou can make: {options}. Pick a video only if it really "
        "needs motion or a story.\nReply ONLY JSON: {\"make\": \"image\" or \"video\", "
        "\"prompt\": \"detailed visual prompt for the image model: subject, setting, style, lighting, vivid colors\", "
        "\"text\": \"short meme text to put on the picture, or empty\", "
        "\"video_idea\": \"one sentence: what the video is about (if video)\", "
        "\"post\": \"1-2 sentence caption in your voice\"}\n" + RULES, temperature=0.95 if not text else 0.7))
    if not d.get("prompt") and not d.get("video_idea"):
        raise RuntimeError("The AI didn't come up with a usable idea; try again.")
    if vid and d.get("make") == "video":
        import mm_aivideo
        print(f"Making a fully-AI video: {d.get('video_idea') or text or 'random'}", flush=True)
        cid = mm_aivideo.make(con, ask, identity, memory, _cfg().get("ollama_model"),
                              hint=text or d.get("video_idea") or None)
        if cid:
            vpath = con.execute("SELECT path FROM mm_clips WHERE id=?", (cid,)).fetchone()[0]
            try:  # "save and open" only: keep it out of the posting queue
                con.execute("UPDATE mm_clips SET status='skipped', hold_reason='made with Custom (not posted)' "
                            "WHERE id=?", (cid,))
            except Exception:
                con.execute("UPDATE mm_clips SET status='skipped' WHERE id=?", (cid,))
            con.commit()
            out = _out("custom_aivid", ".mp4")
            shutil.copy(vpath, out)
            return {"paths": [out], "caption": d.get("post", ""), "note": ""}
        print("[the AI video didn't work out; making a picture instead]")
    note = ""
    if pic:
        mm_images.unload_ollama(_cfg().get("ollama_model"))
        img = mm_images.comfy_generate(d.get("prompt") or d.get("video_idea"), width=1088, height=1360)
        if d.get("text"):
            img = _meme_text(img, d["text"], "")
    else:
        note = ("No AI picture model here, so this is a text graphic. For AI pictures/videos install ComfyUI Desktop "
                "and the Z-Image Turbo files (see 'Custom content' in the README).")
        print(note)
        img = text_card(d.get("text") or text or d.get("post", "") or "...")
    out = _out("custom_new", ".jpg")
    img.save(out, "JPEG", quality=92)
    return {"paths": [out], "caption": d.get("post", ""), "note": note}


# ======================= entry =======================

def make(con, ask, identity, memory, media=None, text=""):
    """Returns {'paths': [...], 'caption': str, 'note': str}."""
    text = (text or "").strip()
    media = (media or "").strip().strip('"') or None
    if media:
        if not os.path.exists(media):
            raise RuntimeError(f"Can't find {media}")
        ext = os.path.splitext(media)[1].lower()
        if ext in IMAGE_EXT:
            print(f"Editing your picture{': ' + text if text else ' (the AI picks the edits)'}", flush=True)
            return make_image(con, ask, identity, media, text)
        if ext in VIDEO_EXT:
            print(f"Editing your video{': ' + text if text else ' (the AI picks the edits)'}", flush=True)
            return make_video(con, ask, identity, media, text)
        raise RuntimeError(f"{ext or 'That file'} isn't a picture or video I can use. Pictures: "
                           f"{' '.join(IMAGE_EXT)}  Videos: {' '.join(VIDEO_EXT)}")
    print(f"Making something new{': ' + text if text else ' (random)'}", flush=True)
    try:
        return make_from_text(con, ask, identity, memory, text)
    finally:
        try:
            mm_images.stop_comfy_if_started()
        except Exception:
            pass
