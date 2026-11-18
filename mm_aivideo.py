"""
Short AI videos: Z-Image Turbo makes a photo -> Wan 2.2 (image-to-video, 14B fp8 + 4-step lightx2v LoRAs)
animates it in ComfyUI -> on-screen text (+ your AI voice line if the voice clone is installed) -> a 9:16 clip
that gets posted like any other clip, ALWAYS labeled as AI (YouTube synthetic flag, captions say AI-generated).
Needs ComfyUI Desktop open. Uses your existing models in "B:\\AI\\Comfy UI\\models".
"""
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone

import requests

import creator_profile as profile

import mm_images
import mm_video

WAN = {"high": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
       "low": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
       "lora_high": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
       "lora_low": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
       "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
       "vae": "wan_2.1_vae.safetensors"}
W, H, FRAMES, FPS = 480, 832, 81, 16  # ~5 seconds at 480p (what a 12GB card can handle)
NEG = ("blurry, low quality, static, still image, distorted, deformed hands, extra limbs, watermark, text, subtitles, "
       "worst quality, jpeg artifacts, ugly, messy background")


def _upload_image(base, path):
    with open(path, "rb") as f:
        r = requests.post(f"{base}/upload/image", files={"image": (os.path.basename(path), f, "image/png")},
                          data={"overwrite": "true"}, timeout=60).json()
    return r["name"]


def wan_animate(image_path, motion_prompt, seed=None, free=True):
    """Image + motion description -> list of PIL frames (81 frames, 480x832)."""
    from io import BytesIO
    from PIL import Image
    base = mm_images.comfy_url()
    if not base:
        raise RuntimeError("ComfyUI isn't running (open ComfyUI Desktop for AI videos)")
    name = _upload_image(base, image_path)
    seed = seed or random.randint(1, 2**31)
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": WAN["high"], "weight_dtype": "default"}},
        "2": {"class_type": "UNETLoader", "inputs": {"unet_name": WAN["low"], "weight_dtype": "default"}},
        "3": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": WAN["lora_high"], "strength_model": 1.0}},
        "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["2", 0], "lora_name": WAN["lora_low"], "strength_model": 1.0}},
        "5": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["3", 0], "shift": 5.0}},
        "6": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 5.0}},
        "7": {"class_type": "CLIPLoader", "inputs": {"clip_name": WAN["clip"], "type": "wan", "device": "default"}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": motion_prompt, "clip": ["7", 0]}},
        "9": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["7", 0]}},
        "10": {"class_type": "VAELoader", "inputs": {"vae_name": WAN["vae"]}},
        "11": {"class_type": "LoadImage", "inputs": {"image": name}},
        "12": {"class_type": "WanImageToVideo", "inputs": {"positive": ["8", 0], "negative": ["9", 0], "vae": ["10", 0],
                                                           "start_image": ["11", 0], "width": W, "height": H,
                                                           "length": FRAMES, "batch_size": 1}},
        "13": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["5", 0], "add_noise": "enable", "noise_seed": seed,
                                                            "steps": 4, "cfg": 1.0, "sampler_name": "euler",
                                                            "scheduler": "simple", "positive": ["12", 0],
                                                            "negative": ["12", 1], "latent_image": ["12", 2],
                                                            "start_at_step": 0, "end_at_step": 2,
                                                            "return_with_leftover_noise": "enable"}},
        "14": {"class_type": "KSamplerAdvanced", "inputs": {"model": ["6", 0], "add_noise": "disable", "noise_seed": seed,
                                                            "steps": 4, "cfg": 1.0, "sampler_name": "euler",
                                                            "scheduler": "simple", "positive": ["12", 0],
                                                            "negative": ["12", 1], "latent_image": ["13", 0],
                                                            "start_at_step": 2, "end_at_step": 10000,
                                                            "return_with_leftover_noise": "disable"}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["10", 0]}},
        "16": {"class_type": "SaveImage", "inputs": {"images": ["15", 0], "filename_prefix": "clip_wan"}},
    }
    r = requests.post(f"{base}/prompt", json={"prompt": wf, "client_id": "clip-machine"}, timeout=60).json()
    if "prompt_id" not in r:
        raise RuntimeError(f"ComfyUI rejected the Wan workflow: {str(r)[:500]}")
    pid = r["prompt_id"]
    try:
        for _ in range(900):  # up to 30 min (first load of the 14B models is slow)
            time.sleep(2)
            h = requests.get(f"{base}/history/{pid}", timeout=20).json().get(pid)
            if h and h.get("outputs", {}).get("16"):
                frames = []
                for im in h["outputs"]["16"]["images"]:
                    raw = requests.get(f"{base}/view", params={"filename": im["filename"], "subfolder": im["subfolder"],
                                                              "type": im["type"]}, timeout=60).content
                    frames.append(Image.open(BytesIO(raw)).convert("RGB"))
                return frames
            if h and h.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"Wan failed in ComfyUI: {str(h['status'])[:500]}")
        raise RuntimeError("Wan took too long (30 min)")
    finally:
        if free:
            free_comfy(base)


def free_comfy(base=None):
    """Give the VRAM back (ComfyUI unloads its models)."""
    try:
        requests.post(f"{base or mm_images.comfy_url()}/free", json={"unload_models": True, "free_memory": True}, timeout=20)
    except Exception:
        pass


MOODS = ("hype", "funny", "chill", "dramatic", "spooky", "retro")
MODES = ("own", "mix", "trending")
_OWN_STUFF = re.compile("|".join([profile.lore_regex() or r"(?!x)x", re.escape(profile.name().lower())]), re.I)
STOP = set("the a an and or of to in on at for with my me i is it this that was be are you your his her its as by from "
           "but not no so just like what who how when why get got have has had we they them our up out into over "
           "ai video".split())
LOCK = "aivideo.lock"


def content_mode():
    """own = your channel's world and lore | trending = no the creator stuff, just whatever is popular | mix = both."""
    import mm_safety
    m = str(mm_safety.config().get("content_mode", "mix")).lower()
    return m if m in MODES else "mix"


def _words(t):
    return {w for w in re.findall(r"[a-z0-9']+", (t or "").lower()) if w not in STOP and len(w) > 2}


def ensure_series(con):
    con.execute("""CREATE TABLE IF NOT EXISTS ai_series (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, premise TEXT,
                   characters TEXT, style TEXT, mode TEXT, episodes INTEGER DEFAULT 0, last_summary TEXT,
                   cliffhanger TEXT, last_clip INTEGER, last_at TEXT, active INTEGER DEFAULT 1)""")
    con.commit()


def _series_score(con, sid):
    """Average result of this series' posted episodes vs the account's usual (None = not judged yet)."""
    r = con.execute("SELECT AVG(p.score), COUNT(*) FROM mm_posts p JOIN mm_clips c ON c.id=p.clip_id WHERE "
                    "c.variant LIKE ? AND p.score IS NOT NULL", (f'%"series": {sid},%',)).fetchone()
    base = con.execute("SELECT AVG(score) FROM mm_posts WHERE score IS NOT NULL").fetchone()[0] or 0
    return None if not r or not r[1] else (r[0] or 0) - base


def _pick_series(con, mode):
    """Continue a series whose last episode is out (and isn't flopping), or None."""
    ensure_series(con)
    rows = con.execute("SELECT id, name, premise, characters, style, episodes, last_summary, cliffhanger, last_clip "
                       "FROM ai_series WHERE active=1 AND (mode=? OR ?='mix') ORDER BY last_at ASC", (mode, mode)).fetchall()
    for r in rows:
        if r[5] >= 6:  # long enough: wrap it up next time and retire it
            con.execute("UPDATE ai_series SET active=0 WHERE id=?", (r[0],))
            continue
        posted = con.execute("SELECT 1 FROM mm_posts WHERE clip_id=?", (r[8],)).fetchone()
        if not posted:
            continue  # the last episode isn't out yet
        sc = _series_score(con, r[0])
        if sc is not None and sc < -0.5 and r[5] >= 2:
            con.execute("UPDATE ai_series SET active=0 WHERE id=?", (r[0],))
            print(f"AI series '{r[1]}' isn't working; retired it")
            continue
        con.commit()
        return r
    con.commit()
    return None


def _repeats(con, cand, series_id=None):
    """Too close to an AI video it already made (titles/concepts), or leaning on the same overused words."""
    recent = con.execute("SELECT c.title, m.text FROM mm_clips c JOIN mm_moments m ON m.id=c.moment_id WHERE "
                         "c.ai_visual=1 ORDER BY c.id DESC LIMIT 15").fetchall()
    if series_id:  # episodes of the same series are supposed to share characters
        recent = [r for r in recent if f'"series": {series_id},' not in (r[1] or "")]
    title, concept = _words(cand.get("title")), _words(f"{cand.get('concept', '')} {cand.get('title', '')}")
    for t, text in recent:
        o = _words(t)
        if title and o and len(title & o) / max(1, min(len(title), len(o))) >= 0.5:
            return f"the title is too close to one you already made ('{t}')"
        oc = _words((text or "").split("|")[0])
        if concept and oc and len(concept & oc) / max(1, min(len(concept), len(oc))) >= 0.55:
            return f"the idea is too close to one you already made ('{t}')"
    worn = _worn(con)
    hit = sorted(worn & title)
    if hit and not series_id:
        return f"it leans on words you've overused lately ({', '.join(hit)})"
    return None


def _worn(con):
    """Words that showed up in 2+ of the last 6 AI video titles (e.g. 'core', 'cursed')."""
    from collections import Counter
    cnt = Counter()
    for (t,) in con.execute("SELECT title FROM mm_clips WHERE ai_visual=1 ORDER BY id DESC LIMIT 6"):
        cnt.update(_words(t))
    return {w for w, n in cnt.items() if n >= 2}


DIRECTOR = ("You're a sharp short-form video director who makes original, scroll-stopping AI videos for TikTok, "
            "YouTube Shorts and Reels. Funny, weird, a little absurd, never mean. You follow trends fast and turn them "
            "into your OWN ideas with your OWN characters.")


def _idea(con, ask, identity, memory, hint, scenes, mode=None):
    """The bot writes a tiny story: hook shot, escalation, punchline (or a series episode). Returns the idea dict."""
    import mm_trends
    import threadsbot as tb
    mode = mode or content_mode()
    if mode == "mix":
        try:
            import mm_brain
            flavor = mm_brain.choose(con, "ai_content", ["own", "trending"], log=False)
        except Exception:
            flavor = random.choice(["own", "trending"])
    else:
        flavor = mode
    try:
        gaming = mm_trends.fresh(con, hours=48, limit=6)
        memes = mm_trends.fresh(con, hours=48, limit=6, memes=True)
        pop = mm_trends.fresh(con, hours=48, limit=8, pop=True)
    except Exception:
        gaming, memes, pop = [], [], []
    # series: continue one, start one, or a one-off (the brain learns which works)
    series = None if hint else _pick_series(con, flavor)
    if series:
        plan = "next_episode"
    else:
        try:
            import mm_brain
            plan = mm_brain.choose(con, "ai_video_plan", ["standalone", "new_series"], log=False)
        except Exception:
            plan = random.choice(["standalone", "new_series"])
        if hint:
            plan = "standalone"
    recent = [r[0] for r in con.execute("SELECT title FROM mm_clips WHERE ai_visual=1 ORDER BY id DESC LIMIT 10") if r[0]]
    worn = sorted(_worn(con))
    if flavor == "own":
        who = (f"{identity}\n\nYOUR MEMORY:\n{memory}\n\n"
               "This one is for your channel: it can use your world and characters (one at most) or a classic gamer "
               "situation (lag, ranked, teammates, boss fights, loot, NPCs).\n")
        topics = ("TRENDING IN GAMING RIGHT NOW: " + " | ".join(gaming) + "\n" if gaming else "") + \
                 ("POPULAR MEMES RIGHT NOW: " + " | ".join(memes) + "\n" if memes else "")
    else:
        who = (f"{DIRECTOR}\n\nThis one has NOTHING to do with [CREATOR]: no recurring characters ([LORE]), "
               "no channel inside jokes. It rides whatever is popular RIGHT NOW (gaming or not) with fresh "
               "original characters. Proven AI formats: talking food/fruit/animal mini-dramas, 'what if X was a "
               "video game', a tiny hero on a quest for a legendary item, an everyday object with a big ego, a "
               "documentary narrator over something ridiculous.\n")
        topics = ("TRENDING RIGHT NOW (anything): " + " | ".join(pop) + "\n" if pop else "") + \
                 ("TRENDING IN GAMING: " + " | ".join(gaming) + "\n" if gaming else "") + \
                 ("POPULAR MEMES: " + " | ".join(memes) + "\n" if memes else "")
    if plan == "next_episode":
        sid, name, premise, chars, style, eps, last, cliff, _ = series
        story = (f"THIS IS EPISODE {eps + 1} OF YOUR SERIES '{name}'. Premise: {premise}. Characters (draw them EXACTLY "
                 f"like this every time): {chars}. Art style: {style}. Last episode: {last}. It ended on: {cliff}. "
                 "Pick up right where it left off, raise the stakes, and end on a NEW cliffhanger"
                 + (" (or wrap the story up with a big payoff, since this is the last episode)" if eps + 1 >= 6 else "")
                 + ". The title must stand on its own: NEVER put 'part', 'episode' or a number in it.\n")
    elif plan == "new_series":
        story = ("START A NEW MINI-SERIES (episode 1 of several): a simple premise with a goal people want to see "
                 "finished (a quest, a rivalry, a mystery, a drama), 1-2 memorable characters, and an ending that is a "
                 "real cliffhanger so viewers come back for the next one. The title must stand on its own: never put "
                 "'part 1', 'episode' or a number in it.\n")
    else:
        story = "A ONE-OFF video: one complete joke with a punchline.\n"
    feedback = ""
    for attempt in range(4):
        raw = ask(
            who + f"\nYou're directing a FULLY AI-MADE vertical video ({scenes} shots, about {scenes * 5} seconds). It "
            "has to stop the scroll in the first second and make sense to a total stranger.\n" + story
            + (f"WHAT [OWNER] ASKED FOR: {hint}\n" if hint else "") + topics
            + ("Videos you already made (do something CLEARLY different): " + " | ".join(recent) + "\n" if recent else "")
            + ("Words you've overused lately, avoid them: " + ", ".join(worn) + "\n" if worn else "")
            + "HOW TO MAKE IT GOOD:\n"
            f"- Shot 1 is the HOOK: the weirdest, most eye-catching image, with text that sets up the story. "
            f"{'The middle shots escalate. ' if scenes > 2 else ''}The last shot is the punchline, twist or cliffhanger.\n"
            "- The same characters and art style in every shot (describe each character fully in EVERY image prompt, "
            "because every image is made separately).\n"
            "- Big, clear subject in the middle of a vertical frame. Bold colors. Stylized 3D / cartoon / claymation look.\n"
            "- On-screen text: max 6 words per shot, plain words.\n"
            "HARD RULES: original characters only. No real people, celebrities, real brands/logos or copyrighted "
            "characters (a trend about a real game or show becomes an original look-alike world, e.g. 'a cartoon "
            "battle-royale island', never the real characters). Nothing gory or sexual, no death or tragedy jokes, "
            "no politics.\n"
            f"{feedback}Reply ONLY JSON: {{\"concept\": \"the idea in one sentence\", \"style\": \"the art style in a few "
            "words\", \"character\": \"the main character(s), described so they can be drawn the same every time\", "
            "\"scenes\": [{\"image\": \"detailed vertical image prompt\", \"motion\": \"what moves in these 5 seconds: one "
            f"action + one simple camera move\", \"text\": \"on-screen text\"}} ... exactly {scenes} scenes], \"voice\": "
            "\"one funny narrator line for the start, max 12 words\", \"title\": \"video title, max 45 characters, says "
            "what happens\", \"caption\": \"one short sentence, a real question about it\", \"mood\": \"one of: "
            f"{', '.join(MOODS)}\", \"cliffhanger\": \"how it ends, in a few words\"" +
            (", \"series_name\": \"a short name for the series\", \"premise\": \"the series premise in one sentence\""
             if plan == "new_series" else "") + "}", temperature=0.9)
        try:
            cand = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            sc = [x for x in cand.get("scenes") or [] if isinstance(x, dict) and x.get("image") and x.get("motion")]
        except Exception:
            feedback = "(Your last reply wasn't valid JSON. Reply with ONLY the JSON object.)\n"
            continue
        if not sc:
            feedback = "(Your last idea had no usable scenes. Every scene needs an image and a motion.)\n"
            continue
        cand["scenes"] = sc[:scenes]
        cand["title"] = re.sub(r"\s*[\(\[\-|:]*\s*(part|pt\.?|episode|ep\.?)\s*#?\d+\s*[\)\]]*", "",
                               str(cand.get("title", "")), flags=re.I).strip(" -|:") or "wait for it"
        why = _repeats(con, cand, series[0] if series else None)
        if not why and flavor != "own" and _OWN_STUFF.search(json.dumps(cand)):
            why = "it used your own channel stuff, and this one must have none"
        if why:
            print(f"AI video idea #{attempt + 1} rejected: {why}")
            feedback = f"(Your last idea was rejected because {why}. Come up with something DIFFERENT.)\n"
            continue
        ok, why = tb.safety_verdict(
            f"Concept: {cand.get('concept', '')} | Spoken line: {cand.get('voice', '')} | Caption: {cand.get('caption', '')} | "
            + " | ".join(f"Shot (cartoon AI art): {x['image']} / text: {x.get('text', '')}" for x in cand["scenes"]))
        if ok:
            cand["_plan"], cand["_flavor"] = plan, flavor
            if series:
                cand["_series"] = series
            return cand
        print(f"AI video idea #{attempt + 1} blocked ({why}); trying another idea")
        feedback = f"(Your last idea was rejected because: {why}. Come up with a different one.)\n"
    return None


def _save_series(con, d, cid):
    """After the video is made: start the series or move it forward one episode. Returns (series id, episode)."""
    ensure_series(con)
    now = datetime.now(timezone.utc).isoformat()
    if d.get("_plan") == "next_episode":
        sid, eps = d["_series"][0], d["_series"][5] + 1
        con.execute("UPDATE ai_series SET episodes=?, last_summary=?, cliffhanger=?, last_clip=?, last_at=? WHERE id=?",
                    (eps, d.get("concept", ""), d.get("cliffhanger", ""), cid, now, sid))
    elif d.get("_plan") == "new_series":
        cur = con.execute("INSERT INTO ai_series (name, premise, characters, style, mode, episodes, last_summary, "
                          "cliffhanger, last_clip, last_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                          ((d.get("series_name") or d.get("title") or "untitled")[:60], d.get("premise") or d.get("concept"),
                           d.get("character", ""), d.get("style", ""), d.get("_flavor"), d.get("concept", ""),
                           d.get("cliffhanger", ""), cid, now))
        sid, eps = cur.lastrowid, 1
    else:
        return None, None
    con.commit()
    return sid, eps


def _locked():
    try:
        return time.time() - os.path.getmtime(LOCK) < 45 * 60
    except OSError:
        return False


def make(con, ask, identity, memory, ollama_model=None, use_voice=None, hint=None, scenes=None):
    """Create one fully-AI video (several shots: AI pictures -> AI animation -> text, AI music, your AI voice) and
    register it as a ready clip. Returns the clip id (or None)."""
    import mm_safety
    import mm_voice
    import mm_words
    base = mm_images.ensure_comfy()
    if not base:
        print("AI video: ComfyUI isn't available, skipping")
        return None
    scenes = max(1, min(5, int(scenes or mm_safety.config().get("ai_video_scenes", 3))))
    if _locked():
        print("AI video: another AI video is being made right now; only one at a time (your graphics card can't do two)")
        return None
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    try:
        return _make(con, ask, identity, memory, ollama_model, use_voice, hint, scenes, base)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def _make(con, ask, identity, memory, ollama_model, use_voice, hint, scenes, base):
    import mm_voice
    import mm_words
    d = _idea(con, ask, identity, memory, hint, scenes)
    if not d:
        print("AI video: no idea passed after 4 tries, skipping this time")
        return None
    cont = d.get("_plan") in ("new_series", "next_episode") and not (d.get("_series") and d["_series"][5] + 1 >= 6)
    next_ep = (d["_series"][5] + 2) if d.get("_series") else 2
    print(f"AI video idea: {d.get('concept') or d.get('title')}", flush=True)
    if ollama_model:
        mm_images.unload_ollama(ollama_model)
    work = os.path.join(mm_video.OUT, f"ai_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}")
    os.makedirs(work, exist_ok=True)
    t0 = time.time()
    ff = mm_video.ffmpeg_exe()
    look = ", ".join(x for x in (d.get("character"), d.get("style"), "vertical composition, vivid colors, sharp, "
                                                                     "high detail") if x)
    shots = d["scenes"]
    photos = []
    for i, sc in enumerate(shots):  # all the pictures first, then all the animation (fewer model swaps)
        print(f"AI video: picture {i + 1}/{len(shots)} (Z-Image)...", flush=True)
        p = os.path.join(work, f"start{i}.png")
        mm_images.comfy_generate(f"{sc['image']}. {look}", width=480, height=832).save(p)
        photos.append(p)
    parts = []
    try:
        for i, sc in enumerate(shots):
            print(f"AI video: animating shot {i + 1}/{len(shots)} (Wan 2.2, a few minutes each)...", flush=True)
            try:
                frames = wan_animate(photos[i], sc["motion"], free=False)
            except Exception as e:
                print(f"[shot {i + 1} failed: {str(e)[:200]}]")
                if parts:
                    break  # keep what we have
                raise
            sd = os.path.join(work, f"s{i}")
            os.makedirs(sd, exist_ok=True)
            for k, fr in enumerate(frames):
                fr.save(os.path.join(sd, f"f{k:04d}.png"))
            ov = os.path.join(work, f"overlay{i}.png")
            mm_images.text_overlay(mm_words.clean(sc.get("text", ""), kind="title") or "", 1080, 1920).save(ov)
            part = os.path.join(work, f"part{i}.mp4")
            # one lonely shot plays forward then backward (a smooth ~10s loop); several shots just cut together
            chain = ("[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0," if len(shots) == 1 else "[0:v]")
            dur = len(frames) / FPS * (2 if len(shots) == 1 else 1)
            last = i == len(shots) - 1
            cmd = [ff, "-y", "-framerate", str(FPS), "-i", os.path.join(sd, "f%04d.png"), "-i", ov]
            graph = f"{chain}scale=1080:1920:flags=lanczos,fps=30[v];[v][1:v]overlay=0:0[o]"
            if last and cont:  # series: a "follow for part N" card over the last 1.8 seconds (never in the title)
                card = os.path.join(work, "endcard.png")
                end_card(f"FOLLOW FOR PART {next_ep}").save(card)
                cmd += ["-i", card]
                graph += f";[o][2:v]overlay=0:0:enable='gte(t,{max(0.0, dur - 1.8):.2f})'[out]"
            else:
                graph += ";[o]null[out]"
            mm_video.run(cmd + ["-filter_complex", graph, "-map", "[out]", "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", "20", "-pix_fmt", "yuv420p", part])
            parts.append(part)
            for fn in os.listdir(sd):
                os.remove(os.path.join(sd, fn))
            os.rmdir(sd)
    finally:
        free_comfy(base)
    lst = os.path.join(work, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("".join(f"file '{os.path.abspath(p)}'\n" for p in parts))
    silent = os.path.join(work, "silent.mp4")
    mm_video.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                  "-map", "0:v", "-map", "1:a", "-shortest", "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", silent])
    for p in parts + [lst]:
        try:
            os.remove(p)
        except OSError:
            pass
    final = os.path.join(work, "final.mp4")
    try:  # its own AI music (it has no sound of its own)
        import mm_audio
        mood = d.get("mood") if d.get("mood") in MOODS else None
        track, mood = mm_audio.pick(con, mood, ollama_model)
        if track:
            withm = os.path.join(work, "music.mp4")
            mm_audio.add_bed(silent, track, withm, solo=True)
            os.replace(withm, silent)
    except Exception as e:
        print(f"[AI video music skipped: {e}]")
    voiced = False
    if (use_voice is None or use_voice) and d.get("voice") and mm_voice.available(con):
        try:
            mm_voice.add_hook(silent, mm_words.clean(d["voice"], kind="title"), final)
            voiced = True
        except Exception as e:
            print(f"[AI voice skipped: {e}]")
    if not voiced:
        os.replace(silent, final)
    title = mm_words.clean(d.get("title") or "AI chaos", kind="title")[:70] or "AI chaos"
    hook = mm_words.clean(shots[0].get("text", ""), kind="title")
    concept = (f"Fully AI video. Joke: {d.get('concept', '')} | " + " | ".join(
        f"Shot {i + 1}: {x['image']} ({x.get('text', '')})" for i, x in enumerate(shots[:len(parts)]))
        + f" | Voice: {d.get('voice', '')} | Caption idea: {d.get('caption', '')}")
    length = mm_video.media_duration(final)
    cur = con.execute("INSERT INTO mm_moments (video_id, start, end, text, loudness, ai_score, hook, title, used) "
                      "VALUES ('ai', 0, ?, ?, 0, 10, ?, ?, 1)", (length, concept, hook, title))
    mid = cur.lastrowid
    cur = con.execute("INSERT INTO mm_clips (moment_id, path, variant, hook, title, ai_voice, ai_visual, created_at) "
                      "VALUES (?,?,?,?,?,?,1,?)", (mid, final, json.dumps({"type": "ai_video", "voice": voiced,
                                                                             "shots": len(parts)}),
                                                   hook, title, 1 if voiced else 0, datetime.now(timezone.utc).isoformat()))
    con.commit()
    cid = cur.lastrowid
    sid, ep = _save_series(con, d, cid)
    v = {"type": "ai_video", "voice": voiced, "shots": len(parts), "mode": d.get("_flavor"), "plan": d.get("_plan")}
    if sid:
        v.update({"series": sid, "episode": ep})
        print(f"AI series: episode {ep} of '{d.get('series_name') or (d['_series'][1] if d.get('_series') else '')}'"
              + (f" (ends with 'follow for part {next_ep}')" if cont else ""))
    con.execute("UPDATE mm_clips SET variant=? WHERE id=?", (json.dumps(v), cid))
    con.execute("UPDATE mm_moments SET text=? WHERE id=?",  # the series id goes in the text too, for _repeats()
                (concept + (f' | "series": {sid},' if sid else ""), mid))
    con.commit()

    print(f"AI video ready in {time.time() - t0:.0f}s ({len(parts)} shots, {length:.0f}s long): {title} ({final})")
    return cid


def end_card(text, w=1080, h=1920):
    """A bold banner near the bottom: 'FOLLOW FOR PART 2'."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f, lines = mm_images._fit_text(d, text, "impact", w - 160, 2, 120)
    y = int(h * 0.70)
    bh = (f.size + 14) * len(lines) + 50
    d.rounded_rectangle((60, y - 25, w - 60, y - 25 + bh), radius=36, fill=(139, 69, 255, 235))
    for line in lines:
        x = (w - d.textlength(line, font=f)) / 2
        d.text((x, y), line, font=f, fill="white", stroke_width=max(3, f.size // 16), stroke_fill="black")
        y += f.size + 14
    return layer
