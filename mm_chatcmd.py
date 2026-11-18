"""
Chat commands: tell the bot what to post from the "Chat with the bot" tab, in plain English.
  "post on threads: my main is the goat"              -> posts that exact text
  "post a threads about the new season"                -> the bot writes it, shows you, posts on "yes"
  "make an instagram meme about ranked"                -> makes it and posts it
  "post an AI image on instagram of a knight at a gas station"      -> AI picture (ComfyUI)
  "post the next clip on youtube and tiktok"           -> posts the next ready clip
  "post clip 12 everywhere" / "post everywhere now"
The bot always shows what it's about to do and waits for "yes" first. Safety + safe-wording filters still apply.
"""
import json
import os
import re

PLATFORMS = ("threads", "instagram", "youtube", "tiktok", "facebook")
SETTING_KEYS = {  # what the owner can change by just saying it
    "platforms.youtube.enabled", "platforms.tiktok.enabled", "platforms.instagram.enabled", "platforms.facebook.enabled",
    "platforms.youtube.max_per_day", "platforms.tiktok.max_per_day", "platforms.instagram.max_per_day",
    "platforms.facebook.max_per_day", "platforms.threads.per_day", "outreach.enabled", "outreach.threads_per_day",
    "ai_video", "ai_videos_per_day", "ai_quiet_minutes", "ai_video_scenes", "source_mode", "content_mode", "self_critique", "instagram_stories", "alerts_enabled", "gpu_mode"}

ROUTER = """You are the assistant that runs [OWNER]'s ([CREATOR]'s) content machine. It posts to Threads, Instagram, YouTube
Shorts, TikTok (inbox drafts [OWNER] posts themselves) and Facebook, makes clips/memes/AI images/AI videos, replies to
comments and learns. In this private chat you are NOT the [CREATOR] character: be a normal, direct, helpful assistant.

Read [OWNER]'s message and decide what they want. They talk casually; understand the meaning, not keywords.
Possible intents:
- "chat": a question or conversation. Answer it from LIVE STATUS (say you don't know if it isn't there).
- "post": he wants something posted. Fill platforms (threads, instagram, youtube, tiktok, facebook; "everywhere" = all),
  kind (text, meme, carousel, ai_image, ai_video, clip, reel, or "any" if he doesn't care), exact (his exact words to
  post, if he gave them), about (what it should be about, if he described it), clip_id (if he named a clip number).
- "full_ai": he wants you to decide and post whatever you think is best (e.g. "post whatever", "do your thing").
- "make": make content WITHOUT posting (kind + about).
- "replies": answer comments right now.
- "cycle": run a full autopilot cycle now.
- "setting": change a setting. Allowed keys: {keys}. Put them in "settings" as {{"key": value}}.
- "remember": a lasting instruction or fact to save (put it in "note").
- "cancel": he's saying no / cancel / stop to something you proposed.
- "confirm": he's saying yes / go / do it to something you proposed.
- "revise": he wants to change the post you just proposed (put the change in "about").
"sure": true only if he clearly told you to do it now (e.g. "post this on threads: ..."); false if it's a maybe,
an idea, or you're guessing. Things that post publicly when you're not sure: set sure=false so you ask first.
"reply": what you say back to him (short, plain; for actions, say what you're doing).

Reply ONLY JSON: {{"intent": "...", "sure": true, "platforms": [], "kind": "", "exact": "", "about": "", "clip_id": null,
"settings": {{}}, "note": "", "reply": ""}}"""


def route(ask, msg, status, memory, history, pending_desc=None):
    """One call: understand what the owner means and what to say back."""
    prompt = (ROUTER.format(keys=", ".join(sorted(SETTING_KEYS))) + f"\n\nLIVE STATUS:\n{status}\n\nMEMORY:\n{memory[:2000]}"
              + (f"\n\nYOU JUST PROPOSED (waiting for his answer): {pending_desc}" if pending_desc else "")
              + "\n\nCONVERSATION SO FAR:\n" + "\n".join(history[-8:]) + f"\n\n[OWNER]'s new message: {msg}")
    raw = ask(prompt, temperature=0.2, max_tokens=400)
    try:
        d = json.loads(re.search(r"\{.*\}", raw or "", re.S).group(0))
    except Exception:
        return {"intent": "chat", "reply": (raw or "").strip()[:600] or "Sorry, I didn't catch that."}
    d["platforms"] = [p for p in (d.get("platforms") or []) if p in PLATFORMS]
    if d.get("intent") == "post" and not d["platforms"]:
        d["platforms"] = ["threads"] if (d.get("kind") in ("text", "", None)) else ["instagram"]
    m = re.search(r"\bclip\s*#?\s*(\d+)", msg, re.I)
    if m:
        d["clip_id"] = int(m.group(1))
    if d.get("kind") == "any":
        d["kind"] = ""
    return d


def apply_settings(changes):
    """Change allowed settings in machine_config.json. Returns what changed."""
    import mm_safety
    cfg = mm_safety.config()
    done = []
    for key, val in (changes or {}).items():
        if key not in SETTING_KEYS:
            done.append(f"(can't change {key} from chat)")
            continue
        d = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        old = d.get(parts[-1])
        if isinstance(old, bool) or isinstance(val, bool):
            val = val if isinstance(val, bool) else str(val).lower() in ("true", "on", "yes", "1")
        elif isinstance(old, (int, float)):
            try:
                val = type(old)(val)
            except (TypeError, ValueError):
                done.append(f"(bad value for {key})")
                continue
        d[parts[-1]] = val
        done.append(f"{key}: {old} -> {val}")
    with open(mm_safety.CONFIG_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(mm_safety.CONFIG_FILE + ".tmp", mm_safety.CONFIG_FILE)
    return done


def other_action(intent, d):
    """full_ai / make / replies / cycle (LIVE)."""
    os.environ["DRY_RUN"] = "0"
    import machine as M
    import threadsbot as tb
    tb.DRY_RUN = False
    c = M.con()
    if intent == "full_ai":
        M.full_ai_control()
    elif intent == "make":
        if d.get("kind") == "ai_video":
            import mm_aivideo
            import mm_safety
            mm_aivideo.make(c, tb.ask, tb.IDENTITY, tb.read_memory(), mm_safety.config().get("ollama_model"),
                            hint=f"[OWNER] asked for: {d.get('about')}" if d.get("about") else None)
        else:
            M.produce(c, force=True)
    elif intent == "replies":
        tb.cmd_thank(auto=True)
        M.reply_instagram(c)
        M.reply_youtube(c)
    elif intent == "cycle":
        M.tick()


def parse(ask, msg):
    """(old keyword version, kept for reference) Plain English -> an action dict."""
    raw = ask(
        "[OWNER] is telling their social media bot to post something. Turn their message into JSON. Platforms: threads, "
        "instagram, youtube, tiktok, facebook ('everywhere' = all of them). Kinds: text (Threads text post), meme, "
        "carousel, ai_image, ai_video, clip (a video clip from his channel), reel.\n"
        "Rules: if he gives the exact words in quotes or after a colon, put them in \"exact\". If he describes what he "
        "wants, put that in \"about\". If he names a clip number, put it in \"clip_id\". If this is NOT a request to "
        "post something, reply {\"action\": \"none\"}.\n"
        "Reply ONLY JSON: {\"action\": \"post\" or \"none\", \"platforms\": [...], \"kind\": \"...\", \"exact\": \"...\", "
        "\"about\": \"...\", \"clip_id\": null}\n\nHis message: " + msg, temperature=0.0, max_tokens=250)
    try:
        d = json.loads(re.search(r"\{.*\}", raw or "", re.S).group(0))
    except Exception:
        return None
    if d.get("action") != "post":
        return None
    plats = [p for p in (d.get("platforms") or []) if p in PLATFORMS]
    if not plats:
        low = msg.lower()
        plats = list(PLATFORMS) if "everywhere" in low else [p for p in PLATFORMS if p in low or
                                                             (p == "instagram" and " ig" in f" {low}")]
    if not plats:
        return None
    d["platforms"] = plats
    m = re.search(r"\bclip\s*#?\s*(\d+)", msg, re.I)
    if m:
        d["clip_id"] = int(m.group(1))
    return d


def describe(d):
    what = (f"the exact text \"{d['exact']}\"" if d.get("exact") else
            (f"a {d.get('kind') or 'post'} about: {d['about']}" if d.get("about") else
             (f"clip {d['clip_id']}" if d.get("clip_id") else "the next ready post")))
    return f"Post {what} on {', '.join(d['platforms'])}"


def draft_threads(ask, d):
    """The Threads text that will go out (exact, or written by the bot about the topic)."""
    import mm_words
    import threadsbot as tb
    if d.get("exact"):
        return mm_words.clean(d["exact"], "threads", "post")
    raw = tb.ask(f"{tb.IDENTITY}\n\nYOUR MEMORY:\n{tb.read_memory()}\n\n[OWNER] (the real [CREATOR]) wants a Threads post about: "
                 f"{d.get('about')}\nWrite ONE post in your voice (max 400 characters, no hashtags, no links). Only the "
                 "post text.", temperature=0.85)
    return mm_words.clean(tb.tidy(raw), "threads", "post")


def run(d, threads_text=None):
    """Do it (LIVE). Returns a summary line per platform."""
    os.environ["DRY_RUN"] = "0"
    import machine as M
    import threadsbot as tb
    tb.DRY_RUN = False
    c = M.con()
    clip = None
    if d.get("clip_id"):
        clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?",
                         (int(d["clip_id"]),)).fetchone()
    out = []
    for p in d["platforms"]:
        try:
            if p == "threads":
                if threads_text:
                    ok, why = tb.safety_verdict(threads_text)
                    if not ok:
                        out.append(f"Threads: blocked by the safety check ({why})")
                        continue
                    tb.save_post(tb.db(), threads_text, "manual", None)
                    out.append("Threads: posted")
                else:
                    M.force_threads()
                    out.append("Threads: posted")
            elif p == "instagram":
                kind = d.get("kind") if d.get("kind") in ("meme", "carousel", "ai_image", "ai_video", "reel") else None
                if kind is None and d.get("about"):
                    kind = "ai_image" if "ai" in (d.get("about") or "").lower() else "meme"
                M.post_instagram(c, force=True, kind=kind, clip=clip, hint=d.get("about"),
                                 caption=d.get("exact") if kind != "reel" else None)
                out.append(f"Instagram: done ({kind or 'auto'})")
            elif p in ("youtube", "tiktok", "facebook"):
                if not clip and d.get("kind") == "ai_video":
                    import mm_aivideo
                    import mm_safety
                    cid = mm_aivideo.make(c, tb.ask, tb.IDENTITY, tb.read_memory(), mm_safety.config().get("ollama_model"),
                                          hint=f"[OWNER] asked for: {d.get('about')}" if d.get("about") else None)
                    clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?",
                                     (cid,)).fetchone() if cid else None
                if not clip and not M.next_clip_for(c, p):
                    print("No clip ready; making one first (a few minutes)...", flush=True)
                    M.produce(c, force=True)
                {"youtube": M.post_youtube, "tiktok": M.post_tiktok, "facebook": M.post_facebook}[p](c, force=True,
                                                                                                    clip=clip)
                out.append(f"{p}: done")
        except (SystemExit, Exception) as e:
            out.append(f"{p}: failed ({str(e)[:150]})")
    return out
