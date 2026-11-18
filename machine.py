"""
CLIP MACHINE
One brain (threadsbot.db + memory.md + channel/keyword files), many platforms.

  python machine.py tick            what the scheduler runs every 15 min:
                                      Threads autopilot -> YouTube stats/learning -> make clips (if GPU allowed)
                                      -> post to YouTube Shorts / TikTok when the safety rules allow
  python machine.py status          what's queued, posted, scores, GPU state
  python machine.py force all|threads|youtube|tiktok|instagram   post NOW, ignoring the schedule
  python machine.py fetch 3         pull 3 more videos from the channel and find moments in them
  python machine.py produce         force-make one clip now (ignores the queue size, respects GPU mode)
  python machine.py setup-youtube   connect the NEW Shorts channel (one time)
  python machine.py setup-tiktok    connect TikTok (one time)
  python machine.py setup-instagram check the Instagram token (one time, after putting INSTAGRAM_TOKEN in secrets.env)
  python machine.py test-picture meme|carousel|ai_image   make one Instagram picture locally to look at
  python machine.py gpu auto|on|off set the GPU toggle

Platforms not connected yet are simply skipped, so Threads keeps working no matter what.
"""
import json
import os
import compat  # Windows/Mac helpers
import creator_profile as profile  # noqa: E402  (profile.json: creator name, handles, lore)
compat.load_env()  # Mac: reads tokens from secrets.env
import sys
import time
from datetime import datetime, timedelta, timezone

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import threadsbot as tb
import mm_gpu
import mm_safety
import mm_tags
import mm_video

LOCK = "machine.lock"


def now():
    return datetime.now(timezone.utc)


def con():
    c = tb.db()
    mm_video.ensure_tables(c)
    mm_video.ensure_library(c)
    for col in ("tags TEXT", "learned INTEGER DEFAULT 0", "title TEXT", "kind TEXT", "media TEXT", "saved INTEGER",
                "real_comments INTEGER", "avg_watch REAL", "skip_rate REAL", "caption TEXT", "done INTEGER DEFAULT 0"):
        try:
            c.execute(f"ALTER TABLE mm_posts ADD COLUMN {col}")
        except Exception:
            pass
    c.commit()
    return c


def score(m):
    """Engagement score for a video post. Retention matters most for Shorts."""
    s = m.get("views", 0) * 0.05 + m.get("likes", 0) * 2 + m.get("comments", 0) * 4 + m.get("shares", 0) * 6
    if m.get("retention") is not None:
        s *= 0.5 + m["retention"] / 100  # 50% avg viewed = x1.0, 100% = x1.5
    return round(s, 2)


# ---------- writing (the creator's voice) ----------
def write_post_text(c, clip, platform):
    """Title, caption, hashtags (+ full-video link). Videos you uploaded yourself keep the title/description you typed;
    blanks get written by the AI from what's in the video."""
    try:
        v = json.loads(clip[2] or "{}") or {}
    except Exception:
        v = {}
    if v.get("type") == "upload" and (v.get("user_title") or v.get("user_caption")):
        t, cap, tags, full = (v.get("user_title"), v.get("user_caption"), None, "")
        if not (t and cap):
            t2, cap2, tags, full = _write_post_text(c, clip, platform)
            t, cap = t or t2, cap if cap is not None and cap != "" else cap2
        if tags is None:
            text = c.execute("SELECT text FROM mm_moments WHERE id=?", (clip[5],)).fetchone()
            tags = mm_tags.make_hashtags(c, tb.ask, platform, f"{t} {cap} | {(text or [''])[0][:600]}")
        write_post_text.last_kind = "upload"
        return mm_tags._cut(t, 100), cap, tags, full
    return _write_post_text(c, clip, platform)


def _write_post_text(c, clip, platform):
    cid, path, variant, hook, title, moment_id = clip
    src = c.execute("SELECT s.video_id, s.title, m.text FROM mm_moments m JOIN mm_sources s ON s.video_id=m.video_id "
                    "WHERE m.id=?", (moment_id,)).fetchone()
    ai = clip_ai(c, cid)
    if not src:  # AI-generated clip: no source video
        mt = c.execute("SELECT text, title FROM mm_moments WHERE id=?", (moment_id,)).fetchone() or ("", title)
        src = (None, mt[1] or title, mt[0] or "")
    import mm_trends
    game = mm_tags.game_name(f"{src[1]} {src[2][:600]}")
    recent_titles = [r[0] for r in c.execute("SELECT title FROM mm_posts WHERE title IS NOT NULL AND title != '' "
                                             "ORDER BY id DESC LIMIT 8")]
    mtype = (c.execute("SELECT mtype FROM mm_moments WHERE id=?", (moment_id,)).fetchone() or [None])[0]
    trend = None
    if mm_trends.should_try(c, platform):
        fr = mm_trends.fresh(c, hours=24, limit=3)
        trend = fr[0] if fr else None
    write_post_text.last_kind = "clip_trend" if trend else "clip"
    trend_line = (f"TRENDING RIGHT NOW: \"{trend}\". Tie the title/caption to this trend if you can make it land "
                  "(a joke, a comparison, a hot take); if it really doesn't fit, ignore it.\n") if trend else ""
    raw = tb.ask(
        f"{tb.IDENTITY}\n\nYOUR MEMORY:\n{tb.read_memory()}\n\n" +
        (f"You're posting a short AI-generated video you made: {src[2][:600]}\n" if not src[0] else
         f"You're posting a short vertical clip cut from your video \"{src[1]}\". What's said in it: {src[2][:600]}\n") +
        f"On-screen hook: {hook}\nWorking title: {title}\n{trend_line}\n"
        f"{tb.recall(f'{title} {hook} {src[2][:300]}', platform)}\n\n"
        + (f"The game is {game}: say its name in the title or the caption if it fits naturally (people search for "
           f"it).\n" if game else "")
        + ("Titles you used lately (make this one clearly different, new words, new shape): "
           + " | ".join(recent_titles) + "\n" if recent_titles else "") +
        f"Write 3 DIFFERENT options for the {platform} title and caption. SHORT wins.\n"
        "TITLE (max 45 characters, no hashtags): say what HAPPENS in the clip, or tease it, so a stranger who has never "
        "seen your channel gets curious. A clear funny claim, a question, or a 'when ...' setup. Do NOT just copy a "
        "random line that was said (only quote it if the line is funny all by itself). Good: 'he lost his leg and kept "
        "playing', 'this is why i don't play support', 'the frog had a badge??'. Bad: 'oh yes he off i'm gonna go'. (Those are only examples of the style; "
        "never reuse them.)\n"
        "CAPTION (ONE short sentence, max 80 characters): a specific question with no single right answer, or a "
        "dumb confident take about THIS clip. Not a yes/no question. Never mention likes, views, the algorithm, scrolling or your stats, and don't drag in characters "
        "that aren't in the clip.\n"
        "Reply ONLY as a JSON list: [{\"title\": \"...\", \"caption\": \"...\"}, ...]", temperature=0.8)
    import re
    try:
        opts = [o for o in json.loads(re.search(r"\[.*\]", raw, re.S).group(0)) if isinstance(o, dict) and o.get("title")]
    except Exception:
        try:
            opts = [json.loads(re.search(r"\{.*?\}", raw, re.S).group(0))]
        except Exception:
            opts = []
    def _stale(t):  # too close to a title we just used
        w = tb._wset(t or "")
        return any(len(w & tb._wset(r)) / max(1, min(len(w), len(tb._wset(r)))) >= 0.6 for r in recent_titles if r)
    keep = [o for o in opts if len(str(o.get("title", ""))) <= 60 and not _stale(str(o.get("title", "")))]
    opts = keep or sorted(opts, key=lambda o: len(str(o.get("title", ""))))[:1]
    data = opts[0] if opts else {"title": title, "caption": hook or ""}
    if len(opts) > 1 and mm_safety.config().get("self_critique", True):
        import mm_content  # self-critic picks the best option and punches it up
        drafts = [f"TITLE: {o.get('title', '')} || CAPTION: {o.get('caption', '')}" for o in opts]
        best, improved, scores = mm_content.critique(tb.ask, tb.IDENTITY, platform, drafts)
        data = opts[best]
        m = re.search(r"TITLE:\s*(.+?)\s*\|\|\s*CAPTION:\s*(.+)", improved or "", re.S)
        if m and len(m.group(1)) <= 60 and len(m.group(1)) <= len(str(data.get("title", ""))) + 5:
            data = {"title": m.group(1).strip(), "caption": m.group(2).strip()}
        print(f"[critic] {platform} options scored {scores} -> #{best + 1}" + (" (punched up)" if m else ""))
    trending = None
    try:
        trending = "; ".join(mm_trends.fresh(c, hours=24, limit=5) + tb.fetch_headlines("gaming_news", c, limit=3))
    except Exception:
        pass
    tags = mm_tags.make_hashtags(c, tb.ask, platform, f"Video: {src[1]} | {data.get('title')} {data.get('caption')} | "
                                                       f"said: {src[2][:500]}", trending, mtype=mtype)
    import mm_words  # platform filters: no begging, no violent/death words, no strong swears in titles
    caption = mm_words.shorten(mm_words.clean(tb.tidy(data.get("caption", "")), platform, "caption"), 110, 1)
    data["title"] = mm_words.clean(tb.tidy(data.get("title") or ""), platform, "title") or mm_words.clean(title, platform,
                                                                                                          "title")
    tags = [t for t in tags if mm_words.ok_hashtag(t)]
    if ai["visual"] or ai["voice"]:  # always disclose AI
        caption = (caption + " " + ("(AI-generated video)" if ai["visual"] else "(AI voice)")).strip()
    full_video = f"Full video: https://youtu.be/{src[0]}" if src[0] else ""
    clean_title = mm_tags.fit_title(platform, tb.tidy(data.get("title") or title), fallback=title or None)
    clean_title = mm_tags._cut(clean_title, 60)  # short titles get read; long ones get cut off in the feed
    return (clean_title, caption, tags, full_video)


def clip_ai(c, clip_id):
    r = c.execute("SELECT COALESCE(ai_visual,0), COALESCE(ai_voice,0) FROM mm_clips WHERE id=?", (clip_id,)).fetchone()
    return {"visual": bool(r and r[0]), "voice": bool(r and r[1])}


# ---------- posting ----------
def next_clip_for(c, platform, source=None):
    """Next clip to post. If both kinds are waiting, the brain picks your own clip or an AI video, based on which
    one beats your usual on this platform (and keeps testing the other one)."""
    q = ("SELECT c.id, c.path, c.variant, c.hook, c.title, c.moment_id FROM mm_clips c WHERE c.status='ready' "
         "AND NOT EXISTS (SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id AND p.platform=?) "
         "AND COALESCE(c.ai_visual,0)=? ORDER BY COALESCE(c.approved,0) DESC, c.id LIMIT 10")
    pools = {"own_clip": c.execute(q, (platform, 0)).fetchall(), "ai_video": c.execute(q, (platform, 1)).fetchall()}
    have = [k for k, v in pools.items() if v]
    if not have:
        return None
    if source not in have:
        import mm_brain
        source = mm_brain.choose(c, f"source@{platform}", have)
    rows = pools[source] + [r for k in have if k != source for r in pools[k]]
    import mm_words
    for clip in rows:
        if (c.execute("SELECT COALESCE(approved,0) FROM mm_clips WHERE id=?", (clip[0],)).fetchone() or [0])[0]:
            return clip  # you said it's good: no second-guessing
        if mm_words.is_sexual(f"{clip[3] or ''} {clip[4] or ''}"):  # sexual words burned into the hook/title
            c.execute("UPDATE mm_clips SET status='skipped', hold_reason='its on-screen text/title has words the "
                      "platforms hold back' WHERE id=?", (clip[0],))
            c.commit()
            print(f"Skipped clip {clip[0]}: its on-screen text/title has sexual words")
            continue
        txt = c.execute("SELECT text FROM mm_moments WHERE id=?", (clip[5],)).fetchone()
        if txt and mm_video.already_made(c, txt[0]):  # the creator already has a Short of this bit
            c.execute("UPDATE mm_clips SET status='duplicate', hold_reason='looked like a moment you already made "
                      "a Short of' WHERE id=?", (clip[0],))
            c.commit()
            print(f"Skipped clip {clip[0]}: you already made a Short of that moment")
            continue
        return clip
    return None


def post_youtube(c, force=False, clip=None):
    import mm_youtube
    ok, why = (True, "forced") if force else mm_safety.may_post(c, "youtube")
    if not ok:
        return print(f"YouTube: not posting ({why})")
    clip = clip or next_clip_for(c, "youtube")
    if not clip:
        return print("YouTube: no ready clip")
    cfg = mm_safety.config()["platforms"]["youtube"]
    title, caption, tags, full = write_post_text(c, clip, "youtube")
    desc = mm_tags.fit_caption("youtube", "\n\n".join(x for x in (caption, full, " ".join(tags)) if x))
    if not os.environ.get("DRY_RUN", "1") == "0":
        return print(f"[DRY RUN] YouTube would upload: {title} | {' '.join(tags)}")
    public = cfg.get("audited", False) and not cfg.get("review_mode", False)
    ai = clip_ai(c, clip[0])
    vid = mm_youtube.upload(clip[1], title, desc, tags, public=public, synthetic=ai["visual"] or ai["voice"])
    c.execute("INSERT INTO mm_posts (clip_id, platform, remote_id, posted_at, privacy, tags, title, kind) VALUES (?,?,?,?,?,?,?,?)",
              (clip[0], "youtube", vid, now().isoformat(), "public" if public else "private", json.dumps(tags), title,
               getattr(write_post_text, "last_kind", "clip")))
    c.commit()
    why = "public" if public else ("PRIVATE for your review: publish it in YouTube Studio" if cfg.get("audited")
                                   else "PRIVATE, locked until the audit passes")
    print(f"YouTube: uploaded https://youtube.com/shorts/{vid} ({why})")
    import mm_notify
    if public:
        mm_notify.posted("YouTube Shorts", title, f"https://youtube.com/shorts/{vid}")
    else:  # private upload: the owner publishes it themselves
        mm_notify.send("YouTube Short ready for you to publish", f"{title}\n\nIt's uploaded as PRIVATE. Open YouTube Studio "
                       "-> Content -> set it to Public.", priority=4, tags=["clapper"],
                       click=f"https://studio.youtube.com/video/{vid}/edit")


def tiktok_text(title, caption, tags):
    """What gets pasted into TikTok: the title line, the caption, then the hashtags."""
    body = title if (caption or "").lower().startswith((title or "").lower()[:25]) else f"{title}\n{caption}".strip()
    return mm_tags.fit_caption("tiktok", "\n".join(x for x in (body, " ".join(tags)) if x))


def tiktok_waiting(c, days=7):
    """TikTok videos that are ready but you haven't posted yet (shown in the app's TikTok tab)."""
    since = (now() - timedelta(days=days)).isoformat()
    return c.execute("SELECT COUNT(*) FROM mm_posts WHERE platform='tiktok' AND privacy IN ('manual','inbox') AND "
                     "COALESCE(done,0)=0 AND COALESCE(media,'') NOT LIKE '%tiktok_video_id%' AND posted_at > ?",
                     (since,)).fetchone()[0]


def _tiktok_inbox(path, text, ai):
    """Push one video to the TikTok inbox on your phone. Returns (publish_id, None) or (None, plain-English reason)."""
    import mm_tiktok
    try:
        return mm_tiktok.upload(path, text, audited=False, ai_generated=ai), None
    except (RuntimeError, SystemExit) as e:
        if "too_many_pending" in str(e):
            return None, ("TikTok is refusing new drafts because too many are still sitting unposted in your TikTok "
                          "inbox. Post or delete some drafts in the TikTok app, then try again (or post this one from "
                          "the app's TikTok tab on your PC).")
        return None, str(e)[:300]


def post_tiktok(c, force=False, clip=None):
    """Until TikTok's audit passes it can't post for you (and inbox drafts can't carry a caption). So the bot gets the
    video + caption READY in the app's TikTok tab and pings your phone; you post it. Optional: also push the video to
    your phone's TikTok inbox (platforms.tiktok.auto_inbox, or the button in the TikTok tab)."""
    ok, why = (True, "forced") if force else mm_safety.may_post(c, "tiktok")
    if not ok:
        return print(f"TikTok: not posting ({why})")
    cfg = mm_safety.config()["platforms"]["tiktok"]
    audited = cfg.get("audited", False)
    waiting = tiktok_waiting(c)
    if not audited and not force and waiting >= int(cfg.get("max_waiting", 4)):
        return print(f"TikTok: {waiting} videos are already waiting for you in the app's TikTok tab; not making more "
                     "until you post (or skip) some")
    clip = clip or next_clip_for(c, "tiktok")
    if not clip:
        return print("TikTok: no ready clip")
    title, caption, tags, _ = write_post_text(c, clip, "tiktok")
    text = tiktok_text(title, caption, tags)
    if not os.environ.get("DRY_RUN", "1") == "0":
        return print(f"[DRY RUN] TikTok would get ready: {text}")
    ai = clip_ai(c, clip[0])
    is_ai = ai["visual"] or ai["voice"]
    pid, privacy, note = None, "manual", ""
    if audited:
        import mm_tiktok
        pid, privacy = mm_tiktok.upload(clip[1], text, audited=True, ai_generated=is_ai), "public"
    elif cfg.get("auto_inbox", False):
        pid, err = _tiktok_inbox(clip[1], text, is_ai)
        privacy = "inbox" if pid else "manual"
        note = "" if pid else f" (couldn't send it to your TikTok inbox: {err})"
    c.execute("INSERT INTO mm_posts (clip_id, platform, remote_id, posted_at, privacy, tags, title, kind, caption) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (clip[0], "tiktok", pid, now().isoformat(), privacy, json.dumps(tags), title,
               getattr(write_post_text, "last_kind", "clip"), text))
    c.commit()
    if audited:
        return print("TikTok: posted")
    _tiktok_notify(text, is_ai, privacy == "inbox")
    print(("TikTok: sent to your phone's TikTok inbox. " if privacy == "inbox" else "TikTok: video + caption are ready. ")
          + "Open the app -> TikTok tab to watch it, copy the caption and post it." + note)


def _tiktok_notify(text, is_ai, in_inbox):
    import mm_notify
    extra = "\n\n(When posting: turn ON 'AI-generated content' in TikTok's settings)" if is_ai else ""
    where = "It's in your TikTok inbox." if in_inbox else "The video is in the Clip Machine app -> TikTok tab."
    sent = mm_notify.send("TikTok video ready: copy this caption", text, priority=4, tags=["movie_camera"],
                          click="https://www.tiktok.com/")
    stamp = datetime.now().strftime("%m-%d_%H%M")  # the caption as a .txt too: tap it, select all, copy
    mm_notify.send_file("TikTok caption file", f"tiktok_caption_{stamp}.txt", text,
                        message=f"Caption for your next TikTok. {where}{extra}", tags=["page_facing_up"])
    try:
        with open("tiktok_captions.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {text.replace(chr(10), ' ')}\n")
    except OSError:
        pass
    return sent


def tiktok_command(c, cmd, post_id):
    """Buttons in the app's TikTok tab: tiktok-send (push to phone inbox), tiktok-recaption (write a new caption),
    tiktok-phone (send the caption to your phone again)."""
    row = c.execute("SELECT p.id, p.clip_id, p.caption, p.title, cl.path FROM mm_posts p LEFT JOIN mm_clips cl ON "
                    "cl.id=p.clip_id WHERE p.id=? AND p.platform='tiktok'", (post_id,)).fetchone()
    if not row:
        return print(f"TikTok: no video number {post_id}")
    pid, clip_id, text, title, path = row
    ai = clip_ai(c, clip_id)
    is_ai = ai["visual"] or ai["voice"]
    if cmd == "tiktok-recaption":
        clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?", (clip_id,)).fetchone()
        if not clip:
            return print("TikTok: that clip isn't in the library anymore")
        title, caption, tags, _ = write_post_text(c, clip, "tiktok")
        text = tiktok_text(title, caption, tags)
        c.execute("UPDATE mm_posts SET title=?, caption=?, tags=? WHERE id=?", (title, text, json.dumps(tags), pid))
        c.commit()
        return print(f"New caption:\n\n{text}")
    text = text or title or ""
    if cmd == "tiktok-phone":
        ok = _tiktok_notify(text, is_ai, False)
        return print(("Sent to your phone (message + .txt file):\n\n" if ok else "Could NOT send (see above):\n\n") + text)
    if cmd == "tiktok-send":
        if not path or not os.path.exists(path):
            return print("TikTok: that video file isn't on the PC anymore")
        rid, err = _tiktok_inbox(path, text, is_ai)
        if not rid:
            return print(f"TikTok: couldn't send it. {err}")
        c.execute("UPDATE mm_posts SET remote_id=?, privacy='inbox' WHERE id=?", (rid, pid))
        c.commit()
        _tiktok_notify(text, is_ai, True)
        print("TikTok: sent to your phone's TikTok inbox (and the caption to your phone). Open TikTok -> inbox -> "
              "paste the caption -> Post.")


def post_facebook(c, force=False, clip=None):
    """Facebook Page Reel (same clips as Shorts/TikTok, free, labeled when AI-made)."""
    import mm_facebook
    blocked = c.execute("SELECT value FROM meta WHERE key='fb_blocked_until'").fetchone()
    if blocked and not force and now() < datetime.fromisoformat(blocked[0]):
        return print("Facebook: waiting for the Page permission (pages_manage_posts); see the app -> Settings -> Facebook check")
    ok, why = (True, "forced") if force else mm_safety.may_post(c, "facebook")
    if not ok:
        return print(f"Facebook: not posting ({why})")
    clip = clip or next_clip_for(c, "facebook")
    if not clip:
        return print("Facebook: no ready clip")
    title, caption, tags, full = write_post_text(c, clip, "facebook")
    text = mm_tags.fit_caption("facebook", "\n\n".join(x for x in (f"{title}. {caption}", full, " ".join(tags)) if x))
    if not os.environ.get("DRY_RUN", "1") == "0":
        return print(f"[DRY RUN] Facebook would post a reel: {text}")
    ai = clip_ai(c, clip[0])
    try:
        vid = mm_facebook.post_reel(clip[1], text, ai_generated=ai["visual"] or ai["voice"])
    except RuntimeError as e:
        if "permission" in str(e).lower():
            c.execute("INSERT OR REPLACE INTO meta VALUES ('fb_blocked_until', ?)", ((now() + timedelta(hours=12)).isoformat(),))
            c.commit()
            import mm_notify
            mm_notify.send("Clip Machine: Facebook needs one permission", "Facebook Reels need the pages_manage_posts "
                           "permission on the Page token. Ask Claude to walk you through it.", tags=["key"], con=c,
                           dedupe_hours=72)
        raise
    c.execute("INSERT INTO mm_posts (clip_id, platform, remote_id, posted_at, privacy, tags, title, kind) VALUES (?,?,?,?,?,?,?,?)",
              (clip[0], "facebook", vid, now().isoformat(), "public", json.dumps(tags), title,
               getattr(write_post_text, "last_kind", "clip")))
    c.commit()
    print(f"Facebook: posted a reel ({vid})")
    import mm_notify
    mm_notify.posted("Facebook", title, profile.get("facebook_page_url") or "")


def update_fb_stats(c):
    """Facebook Reels stats every 2h (plays, likes, comments, shares, watch time)."""
    import mm_facebook
    last = c.execute("SELECT value FROM meta WHERE key='fb_stats_at'").fetchone()
    if last and now() - datetime.fromisoformat(last[0]) < timedelta(hours=2):
        return
    for pid, rid in c.execute("SELECT id, remote_id FROM mm_posts WHERE platform='facebook' AND posted_at > ?",
                              ((now() - timedelta(days=30)).isoformat(),)).fetchall():
        try:
            m = mm_facebook.stats(rid)
        except Exception as e:
            print(f"[facebook stats {rid}] {e}")
            continue
        s = score(m)
        retention = None
        length = _clip_length(c, pid)
        if m.get("avg_watch") and length:
            retention = round(min(100.0, m["avg_watch"] / length * 100), 1)
            s = round(s * (0.5 + retention / 100), 2)
        c.execute("UPDATE mm_posts SET views=?, likes=?, comments=?, shares=?, score=?, retention=?, avg_watch=?, "
                  "checked_at=? WHERE id=?", (m["views"], m["likes"], m["comments"], m["shares"], s, retention,
                                              m.get("avg_watch"), now().isoformat(), pid))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('fb_stats_at', ?)", (now().isoformat(),))
    c.commit()


IG_KINDS = ("reel", "meme", "carousel", "ai_image", "ai_video", "slideshow")


def pick_ig_kind(c, available):
    """The brain picks (discounted Thompson sampling on how each kind does vs your usual on Instagram)."""
    import mm_brain
    return mm_brain.choose(c, "ig_kind", available)


def post_instagram(c, force=False, kind=None, clip=None, moment_id=None, hint=None, caption=None):
    import mm_images
    import mm_instagram
    ok, why = (True, "forced") if force else mm_safety.may_post(c, "instagram")
    if not ok:
        return print(f"Instagram: not posting ({why})")
    reel_clip = clip or c.execute(
        "SELECT c.id, c.path, c.variant, c.hook, c.title, c.moment_id FROM mm_clips c WHERE c.status='ready' "
        "AND COALESCE(c.ai_visual,0)=0 AND EXISTS (SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id AND p.platform='youtube') "
        "AND NOT EXISTS (SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id AND p.platform='instagram') "
        "ORDER BY COALESCE(c.approved,0) DESC, c.id LIMIT 1").fetchone()
    ai_clip = clip if clip and clip_ai(c, clip[0])["visual"] else c.execute(
        "SELECT c.id, c.path, c.variant, c.hook, c.title, c.moment_id FROM mm_clips c WHERE c.status='ready' "
        "AND c.ai_visual=1 AND NOT EXISTS (SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id AND p.platform='instagram') "
        "ORDER BY c.id LIMIT 1").fetchone()
    if clip and ai_clip and clip[0] == ai_clip[0]:
        reel_clip = None
    import mm_words
    if reel_clip and mm_words.is_sexual(f"{reel_clip[3] or ''} {reel_clip[4] or ''}") and not \
            (c.execute("SELECT COALESCE(approved,0) FROM mm_clips WHERE id=?", (reel_clip[0],)).fetchone() or [0])[0]:
        c.execute("UPDATE mm_clips SET status='skipped', hold_reason='its on-screen text/title has words the platforms "
                  "hold back' WHERE id=?", (reel_clip[0],))
        c.commit()
        reel_clip = None
    gpu_ok, _ = mm_gpu.gpu_allowed()
    available = [k for k in IG_KINDS if (k != "reel" or reel_clip) and (k != "ai_video" or ai_clip)
                 and (k != "ai_image" or (gpu_ok and mm_images.comfy_available()))]
    if kind == "reel" and not reel_clip and ai_clip:
        kind = "ai_video"  # "post this clip as a reel" + it's an AI clip
    import mm_content
    ig_idea = None if kind else mm_content.next_idea(c, "instagram")
    if ig_idea:  # a winner to remix or a fan comment to turn into a picture
        want = (ig_idea[2].get("kind") or "meme").replace("_trend", "")
        kind = want if want in ("meme", "carousel", "ai_image") and want in available else "meme"
        print(f"Instagram: using a queued {ig_idea[1]} idea", flush=True)
    if kind:
        available = [k for k in available if k == kind] or available
    tried = []
    while available:
        kind = pick_ig_kind(c, available)
        available.remove(kind)
        tried.append(kind)
        if kind == "slideshow":  # carousel pictures -> a Reel with the bot's AI music + your cloned voice
            import mm_audio
            car = mm_images.make_carousel(c, tb.ask, tb.IDENTITY, tb.read_memory() + (
                f"\n\n[OWNER] ASKED FOR THIS POST: {hint}" if hint else ""), moment_id=moment_id)
            if not car:
                continue
            line = tb.tidy(tb.ask(f"{tb.IDENTITY}\n\nOne short funny line (max 12 words) you'd say out loud over this "
                                  f"slideshow: {car.get('onimage', '')[:300]}. Only the line.", temperature=0.9))
            import mm_words
            line = mm_words.clean(line, "instagram", "title")
            track, mood = mm_audio.pick(c, None, mm_safety.config().get("ollama_model"))
            out = os.path.join("media", "images", f"slideshow_{datetime.now():%Y%m%d%H%M%S}.mp4")
            mm_audio.slideshow(car["paths"], out, track, line)
            import mm_voice
            item = {"kind": "slideshow", "style": f"{len(car['paths'])}pics+{mood if track else 'nomusic'}"
                    + ("+voice" if line and mm_voice.available() else ""), "paths": [out], "caption": car["caption"],
                    "about": car["about"], "ai": bool(track or (line and mm_voice.available())), "video": True,
                    "ai_label": " + ".join(x for x in ("AI voice" if line and mm_voice.available() else "",
                                                        "AI music" if track else "") if x),
                    "onimage": car.get("onimage", "")}
        elif kind in ("reel", "ai_video"):
            rc = reel_clip if kind == "reel" else ai_clip
            title, caption, tags, full = write_post_text(c, rc, "instagram")
            item = {"kind": kind, "style": "clip" if kind == "reel" else "ai_video", "paths": [rc[1]],
                    "caption": f"{title}. {caption}", "about": title, "ai": kind == "ai_video", "clip_id": rc[0],
                    "tags": tags, "video": True}
        else:
            maker = mm_images.KINDS[kind]
            import mm_trends
            trend = None
            if mm_trends.should_try(c, "instagram") and not hint:
                fr = mm_trends.fresh(c, hours=24, limit=3)
                trend = fr[0] if fr else None
            mem = tb.read_memory() + (f"\n\nTRENDING RIGHT NOW: \"{trend}\". Make this post riff on that trend if you can "
                                      "make it land with your content; if it doesn't fit, ignore it." if trend else "")
            if ig_idea:
                mem += "\n" + mm_content.idea_prompt(ig_idea[1], ig_idea[2])
            if hint:
                mem += f"\n\n[OWNER] (the real [CREATOR]) ASKED FOR THIS POST, follow it exactly: {hint}"
            args = (c, tb.ask, tb.IDENTITY, mem)
            item = maker(*args, mm_safety.config().get("ollama_model")) if kind == "ai_image" \
                else maker(*args, moment_id=moment_id)
            if not item:
                continue
            if trend:
                item["kind"] = item["kind"] + "_trend"
        break
    else:
        return print(f"Instagram: nothing to post right now (tried {', '.join(tried) or 'nothing'})")
    if caption:
        item["caption"] = caption
    if "tags" not in item:
        trending = None
        try:
            trending = "; ".join(tb.fetch_headlines("gaming_news", c, limit=5))
        except Exception:
            pass
        item["tags"] = mm_tags.make_hashtags(c, tb.ask, "instagram", f"{item['caption']} {item['about']}", trending)
    import mm_words
    item["caption"] = mm_words.clean(item.get("caption", ""), "instagram", "caption")
    item["tags"] = [t for t in item.get("tags", []) if mm_words.ok_hashtag(t)]
    label = (f"{item['ai_label']}. " if item.get("ai_label") else
             ("" if "AI-generated" in item["caption"] else "AI-generated video. ")) if item.get("video") and item["ai"] \
        else ("AI-generated image. " if item["ai"] else "")
    text = mm_tags.fit_caption("instagram", f"{tb.tidy(item['caption'])}\n\n{label}{' '.join(item['tags'])}")
    if not tb.safety_check(f"{text}\nText/scene on the image: {item.get('onimage', '')}"):
        return print(f"Instagram: dropped a {item['kind']} that failed the safety check")
    if not os.environ.get("DRY_RUN", "1") == "0":
        return print(f"[DRY RUN] Instagram would post a {item['kind']} ({len(item['paths'])} file(s)): {text}")
    if item.get("video") or item["kind"] == "reel":
        mid = mm_instagram.post_reel(item["paths"][0], text)
    else:
        mid = mm_instagram.post_images(item["paths"], text)
    c.execute("INSERT INTO mm_posts (clip_id, platform, remote_id, posted_at, privacy, tags, title, kind, media) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (item.get("clip_id"), "instagram", mid, now().isoformat(), "public", json.dumps(item["tags"]),
               tb.tidy(item["caption"])[:95], item["kind"], json.dumps({"style": item["style"], "paths": item["paths"]})))
    c.commit()
    print(f"Instagram: posted a {item['kind']} ({item['style']}) id {mid}")
    import mm_notify
    mm_notify.posted("Instagram", f"{item['kind']}: {tb.tidy(item['caption'])[:200]}", f"https://www.instagram.com/{profile.get('instagram_username') or ''}/")
    if ig_idea:
        mm_content.use_idea(c, ig_idea[0])
    if mm_safety.config().get("instagram_stories", True):
        try:  # same thing to the Story for extra reach
            mm_instagram.post_story(item["paths"][0], is_video=bool(item.get("video")) or item["kind"] == "reel")
            print("Instagram: also shared to your Story")
        except Exception as e:
            print(f"[Instagram story skipped] {e}")


def _clip_length(c, post_id):
    """Seconds of the video behind a post (cached in mm_clips.description as 'len=12.3')."""
    r = c.execute("SELECT c.id, c.path, c.description FROM mm_posts p JOIN mm_clips c ON c.id=p.clip_id WHERE p.id=?",
                  (post_id,)).fetchone()
    if not r:
        return None
    if r[2] and r[2].startswith("len="):
        return float(r[2][4:])
    if r[1] and os.path.exists(r[1]):
        d = mm_video.media_duration(r[1])
        if d:
            c.execute("UPDATE mm_clips SET description=? WHERE id=?", (f"len={d:.1f}", r[0]))
            return d
    return None


def update_ig_stats(c):
    """Instagram stats every 2h; one lesson per post into the shared memory once it's 48h old."""
    last = c.execute("SELECT value FROM meta WHERE key='ig_stats_at'").fetchone()
    if last and now() - datetime.fromisoformat(last[0]) < timedelta(hours=2):
        return
    rows = c.execute("SELECT id, remote_id, posted_at, learned, kind, media, title, real_comments FROM mm_posts "
                     "WHERE platform='instagram' AND posted_at > ?", ((now() - timedelta(days=30)).isoformat(),)).fetchall()
    if not rows:
        return
    import mm_instagram
    learned = []
    for pid, rid, posted_at, already, kind, media, title, real in rows:
        video = (kind or "").startswith(("reel", "ai_video"))
        try:
            m = mm_instagram.stats(rid, reel=video)
        except Exception as e:
            print(f"[instagram stats {rid}] {e}")
            continue
        honest = m["comments"] if real is None else min(real, m["comments"])
        s = round(m["views"] * 0.05 + m["likes"] * 2 + honest * 4 + m["shares"] * 6 + m["saved"] * 5, 2)
        retention = None
        if video and m.get("avg_watch"):  # % of the reel people watch on average (the strongest Reels signal)
            length = _clip_length(c, pid)
            if length:
                retention = round(min(100.0, m["avg_watch"] / length * 100), 1)
                s = round(s * (0.5 + retention / 100), 2)
        c.execute("UPDATE mm_posts SET views=?, likes=?, comments=?, shares=?, saved=?, score=?, checked_at=?, "
                  "retention=?, avg_watch=?, skip_rate=? WHERE id=?",
                  (m["views"], m["likes"], m["comments"], m["shares"], m["saved"], s, now().isoformat(), retention,
                   m.get("avg_watch"), m.get("skip_rate"), pid))
        if not already and now() - datetime.fromisoformat(posted_at) > timedelta(hours=48):
            style = json.loads(media or "{}").get("style", "")
            c.execute("UPDATE mm_posts SET learned=1 WHERE id=?", (pid,))
            learned.append((f"[Instagram {kind}/{style}] {title} | {m['views']} views, {m['likes']} likes, "
                            f"{m['saved']} saves", "instagram", s, []))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('ig_stats_at', ?)", (now().isoformat(),))
    c.commit()
    if learned:
        tb.reflect(c, learned)


def _real_comments(c, platform, remote_id, authors):
    """Honest comment count for the reward: other people's comments only (the bot's own replies don't count) and
    max 3 per person per post (one person spamming 50 comments isn't 50x the success)."""
    from collections import Counter
    real = sum(min(3, n) for a, n in Counter(a for a in authors if a).items())
    c.execute("UPDATE mm_posts SET real_comments=? WHERE platform=? AND remote_id=?", (real, platform, remote_id))
    c.commit()


def reply_instagram(c, limit=60):
    """Reply to EVERY new comment on our recent Instagram posts (one job at a time)."""
    with tb.reply_lock("ig_reply") as free:
        if not free:
            return print("Instagram replies: another reply job is running; skipping")
        _reply_instagram(c, limit)


def _reply_instagram(c, limit=60):
    import mm_instagram
    c.execute("""CREATE TABLE IF NOT EXISTS ig_answered (comment_id TEXT PRIMARY KEY, username TEXT, text TEXT,
                 reply TEXT, at TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS reply_attempts (comment_id TEXT PRIMARY KEY, tries INTEGER)")
    me = (mm_instagram._load().get("ig_username") or profile.get("instagram_username") or "").lower()
    posts = c.execute("SELECT remote_id, title FROM mm_posts WHERE platform='instagram' AND posted_at > ? "
                      "ORDER BY posted_at DESC", ((now() - timedelta(days=14)).isoformat(),)).fetchall()
    done = 0
    for media_id, title in posts:
        try:
            items = mm_instagram.comments(media_id)
        except Exception as e:
            print(f"[instagram comments {media_id}] {e}")
            continue
        _real_comments(c, "instagram", media_id, [(x.get("username") or "").lower() for x in items
                                                  if (x.get("username") or "").lower() != me])
        for cm in items:
            if done >= limit:
                break
            user = (cm.get("username") or "").lower()
            replied_users = {(r.get("username") or "").lower() for r in cm.get("replies", {}).get("data", [])}
            if not cm.get("text") or user == me or me in replied_users:
                continue
            if c.execute("SELECT 1 FROM ig_answered WHERE comment_id=?", (cm["id"],)).fetchone():
                continue
            draft, why = tb.write_reply("Instagram", title, cm.get("username") or user, cm["text"], max_chars=500)
            if not draft:
                print(f"[Instagram: no reply to @{user} yet ({why}); trying again next run]")
                continue
            if os.environ.get("DRY_RUN", "1") != "0":
                print(f"[DRY RUN] Instagram reply to @{user}: {draft}")
                continue
            mm_instagram.reply(cm["id"], f"@{cm.get('username')} {draft}")
            tb.remember_person("instagram", cm.get("username") or user, cm["text"], draft)
            c.execute("INSERT INTO ig_answered VALUES (?,?,?,?,?)", (cm["id"], user, cm["text"], draft, now().isoformat()))
            c.commit()
            print(f"Instagram: replied to @{user}: {draft}")
            done += 1
    if done:
        print(f"Instagram: {done} repl{'y' if done == 1 else 'ies'} sent")


def reply_youtube(c, limit=60):
    """Reply to EVERY new comment on our Shorts (needs the comment permission: reconnect YouTube once)."""
    with tb.reply_lock("yt_reply") as free:
        if not free:
            return print("YouTube replies: another reply job is running; skipping")
        _reply_youtube(c, limit)


def _reply_youtube(c, limit=60):
    import mm_youtube
    import mm_notify
    if not mm_youtube.has_scope("youtube.force-ssl"):
        mm_notify.send("Clip Machine: one-time step", "To let the bot reply to YouTube comments, open the app -> Settings -> "
                       "Connect YouTube Shorts channel, and allow it again.", tags=["key"], con=c, dedupe_hours=72)
        return print("YouTube replies: waiting for you to reconnect YouTube once (adds the comment permission)")
    c.execute("CREATE TABLE IF NOT EXISTS yt_answered (comment_id TEXT PRIMARY KEY, username TEXT, text TEXT, reply TEXT, at TEXT)")
    me = mm_safety.config()["shorts_channel_id"]
    posts = c.execute("SELECT remote_id, title FROM mm_posts WHERE platform='youtube' AND posted_at > ? ORDER BY posted_at DESC",
                      ((now() - timedelta(days=30)).isoformat(),)).fetchall()
    done = 0
    for vid, title in posts:
        try:
            threads = mm_youtube.comment_threads(vid)
        except Exception as e:
            if "commentsDisabled" in str(e) or "forbidden" in str(e).lower():
                continue  # private video / comments off
            raise
        _real_comments(c, "youtube", vid, [th["snippet"]["topLevelComment"]["snippet"].get("authorChannelId", {}).get("value")
                                           for th in threads if th["snippet"]["topLevelComment"]["snippet"]
                                           .get("authorChannelId", {}).get("value") != me])
        for th in threads:
            if done >= limit:
                break
            top = th["snippet"]["topLevelComment"]["snippet"]
            author = top.get("authorDisplayName", "someone").lstrip("@")
            if top.get("authorChannelId", {}).get("value") == me:
                continue
            mine = [r for r in th.get("replies", {}).get("comments", [])
                    if r["snippet"].get("authorChannelId", {}).get("value") == me]
            if mine or c.execute("SELECT 1 FROM yt_answered WHERE comment_id=?", (th["id"],)).fetchone():
                continue
            draft, why = tb.write_reply("YouTube", title, author, top.get("textOriginal") or top.get("textDisplay", ""),
                                        max_chars=500)
            if not draft:
                print(f"[YouTube: no reply to @{author} yet ({why}); trying again next run]")
                continue
            if os.environ.get("DRY_RUN", "1") != "0":
                print(f"[DRY RUN] YouTube reply to @{author}: {draft}")
                continue
            mm_youtube.reply_comment(th["id"], draft)
            tb.remember_person("youtube", author, top.get("textOriginal") or "", draft)
            c.execute("INSERT INTO yt_answered VALUES (?,?,?,?,?)", (th["id"], author, top.get("textOriginal"), draft,
                                                                     now().isoformat()))
            c.commit()
            print(f"YouTube: replied to @{author}: {draft}")
            done += 1


def update_tiktok_stats(c):
    """Every 2h: TikTok followers/likes + each recent video's views/likes/comments/shares. Matches the clips the bot
    sent to your inbox (once you post them) so they get scored and learned from like everything else."""
    import re as _re
    import mm_tiktok
    last = c.execute("SELECT value FROM meta WHERE key='tt_stats_at'").fetchone()
    if last and now() - datetime.fromisoformat(last[0]) < timedelta(hours=2):
        return
    try:
        acct = mm_tiktok.account_stats()
        vids = mm_tiktok.recent_videos(60, since=time.time() - 15 * 86400)
    except RuntimeError as e:
        if "scope" in str(e).lower() or "permission" in str(e).lower() or "access_token" in str(e).lower():
            import mm_notify
            mm_notify.send("Clip Machine: one-time step", "To track TikTok stats, open the app -> Settings -> Connect TikTok "
                           "and allow it again (adds the stats permission).", tags=["key"], con=c, dedupe_hours=72)
            return print("TikTok stats: reconnect TikTok once to allow stats")
        raise
    c.execute("INSERT OR REPLACE INTO meta VALUES ('tiktok_account', ?)", (json.dumps({
        "followers": acct.get("follower_count", 0), "likes_total": acct.get("likes_count", 0),
        "views": sum(v.get("view_count", 0) for v in vids), "likes": sum(v.get("like_count", 0) for v in vids),
        "comments": sum(v.get("comment_count", 0) for v in vids), "shares": sum(v.get("share_count", 0) for v in vids)}),))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('tiktok_videos', ?)", (json.dumps([
        {"id": v["id"], "t": v.get("create_time", 0), "views": v.get("view_count", 0), "likes": v.get("like_count", 0),
         "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)} for v in vids]),))
    words = lambda t: set(_re.findall(r"[a-z0-9']{4,}", (t or "").lower()))
    for pid, posted_at, title, media in c.execute("SELECT id, posted_at, title, media FROM mm_posts WHERE platform='tiktok'").fetchall():
        info = json.loads(media or "{}")
        vid = next((v for v in vids if v["id"] == info.get("tiktok_video_id")), None)
        if not vid:  # match a newly posted TikTok to the clip we sent: posted after we sent it + similar caption
            t0 = datetime.fromisoformat(posted_at).timestamp()
            cands = [v for v in vids if v.get("create_time", 0) >= t0 - 60 and
                     len(words(title) & words((v.get("title") or "") + " " + (v.get("video_description") or ""))) >= 2]
            vid = min(cands, key=lambda v: v["create_time"]) if cands else None
            if vid:
                info["tiktok_video_id"] = vid["id"]
        if vid:
            m = {"views": vid.get("view_count", 0), "likes": vid.get("like_count", 0),
                 "comments": vid.get("comment_count", 0), "shares": vid.get("share_count", 0)}
            c.execute("UPDATE mm_posts SET views=?, likes=?, comments=?, shares=?, score=?, checked_at=?, media=? WHERE id=?",
                      (m["views"], m["likes"], m["comments"], m["shares"], score(m), now().isoformat(), json.dumps(info), pid))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('tt_stats_at', ?)", (now().isoformat(),))
    c.commit()
    print(f"TikTok stats: {acct.get('follower_count', 0)} followers, {len(vids)} recent videos checked")


def platform_snapshot(c):
    """What the bot sees about its YouTube/TikTok/Instagram results (Threads has its own snapshot)."""
    lines = []
    for plat in ("youtube", "instagram", "tiktok"):
        rows = c.execute("SELECT COALESCE(kind,'clip'), COALESCE(title,''), views, likes, comments, score FROM mm_posts "
                         "WHERE platform=? ORDER BY posted_at DESC LIMIT 5", (plat,)).fetchall()
        if not rows:
            continue
        tot = c.execute("SELECT COUNT(*), COALESCE(SUM(views),0), COALESCE(SUM(likes),0), COALESCE(SUM(comments),0) "
                        "FROM mm_posts WHERE platform=?", (plat,)).fetchone()
        lines.append(f"{plat.upper()}: {tot[0]} posts, {tot[1]} views, {tot[2]} likes, {tot[3]} comments total"
                     + (" (TikTok stats aren't readable; drafts are posted by hand)" if plat == "tiktok" else "")
                     + (" (YouTube uploads are private until the API review passes, so views stay 0)" if plat == "youtube" else ""))
        for k, t, v, l, cm, sc in rows:
            lines.append(f"   - {k}: \"{t[:60]}\" -> {v or 0} views, {l or 0} likes, {cm or 0} comments")
    return "\n".join(lines)


# ---------- all-platform scoreboard ----------
W = {"views": 0.05, "likes": 2, "comments": 4, "shares": 6, "saved": 5}  # engagement only; followers judged by change


def _followers(c, plat):
    """Follower/subscriber counts, refreshed every 2h (API calls)."""
    key = f"followers_{plat}"
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    at = c.execute("SELECT value FROM meta WHERE key=?", (key + "_at",)).fetchone()
    if at and now() - datetime.fromisoformat(at[0]) < timedelta(hours=2):
        return int(row[0]) if row else None
    n = None
    try:
        if plat == "instagram":
            import mm_instagram
            n = int(mm_instagram._get(mm_instagram._uid(), fields="followers_count").get("followers_count", 0))
        elif plat == "youtube":
            import mm_youtube
            it = mm_youtube.service().channels().list(part="statistics", mine=True).execute().get("items", [])
            n = int(it[0]["statistics"].get("subscriberCount", 0)) if it else None
        elif plat == "facebook":
            import mm_facebook
            n = mm_facebook.followers()
    except (SystemExit, Exception) as e:
        print(f"[{plat} followers unavailable] {type(e).__name__}")
    if n is not None:
        c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(n)))
        c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key + "_at", now().isoformat()))
        c.commit()
    return n if n is not None else (int(row[0]) if row else None)


def week_start(offset=0):
    """Monday 00:00 (your local time) of this week (offset=0) or an earlier week (offset=-1 = last week)."""
    t = datetime.now().astimezone()
    monday = (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday + timedelta(weeks=offset)


def _window(c, plat, start, end):
    """Totals for posts made between start and end (only content from that week counts)."""
    s_iso, e_iso = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    if plat == "threads":
        v = c.execute("SELECT COALESCE(SUM(views),0), COALESCE(SUM(likes),0), COALESCE(SUM(replies),0), "
                      "COALESCE(SUM(reposts),0)+COALESCE(SUM(quotes),0), 0, COUNT(*) FROM posts "
                      "WHERE deleted=0 AND created_at >= ? AND created_at < ?", (s_iso, e_iso)).fetchone()
    elif plat == "tiktok":
        row = c.execute("SELECT value FROM meta WHERE key='tiktok_videos'").fetchone()
        vids = [x for x in json.loads(row[0]) if start.timestamp() <= x["t"] < end.timestamp()] if row else []
        v = (sum(x["views"] for x in vids), sum(x["likes"] for x in vids), sum(x["comments"] for x in vids),
             sum(x["shares"] for x in vids), 0, len(vids))
    else:
        v = c.execute("SELECT COALESCE(SUM(views),0), COALESCE(SUM(likes),0), "
                      "COALESCE(SUM(MIN(COALESCE(real_comments, comments), COALESCE(comments, 0))),0), "
                      "COALESCE(SUM(shares),0), COALESCE(SUM(saved),0), COUNT(*) FROM mm_posts "
                      "WHERE platform=? AND posted_at >= ? AND posted_at < ?", (plat, s_iso, e_iso)).fetchone()
    m = dict(views=v[0], likes=v[1], comments=v[2], shares=v[3], saved=v[4], posts=v[5])
    m["score"] = round(sum(m.get(k, 0) * w for k, w in W.items()), 1)
    return m


def _current_followers(c, plat):
    if plat == "threads":
        r = c.execute("SELECT followers FROM account_stats ORDER BY id DESC LIMIT 1").fetchone()
        return (r[0] or 0) if r else 0
    if plat == "tiktok":
        r = c.execute("SELECT value FROM meta WHERE key='tiktok_account'").fetchone()
        return json.loads(r[0]).get("followers", 0) if r else 0
    return _followers(c, plat) or 0


def _meta(c, key, default=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def _set_meta(c, key, val):
    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(val)))


def week_results(c, plat):
    """(this week so far, last week's final, followers gained this week, last week's follower gain)."""
    this_start, last_start = week_start(0), week_start(-1)
    wk, lwk = this_start.date().isoformat(), last_start.date().isoformat()
    fol = _current_followers(c, plat)
    if _meta(c, f"week_followers:{plat}:{wk}") is None:  # first run of the week: remember where followers started
        _set_meta(c, f"week_followers:{plat}:{wk}", fol)
    final = _meta(c, f"week_final:{plat}:{lwk}")
    if final is None:  # last week just ended: freeze its totals so this week is compared with a finished week
        final = _window(c, plat, last_start, this_start)
        f0 = _meta(c, f"week_followers:{plat}:{lwk}")
        final["followers_gained"] = (fol - f0) if f0 is not None else None
        _set_meta(c, f"week_final:{plat}:{lwk}", final)
    cur = _window(c, plat, this_start, datetime.now().astimezone() + timedelta(minutes=1))
    return cur, final, fol - _meta(c, f"week_followers:{plat}:{wk}", fol), final.get("followers_gained")


def update_scoreboard(c):
    """WEEKLY scoreboard: only posts made THIS week (Monday to Sunday) count, compared with last week's final
    numbers. Every Monday it resets. Written to scoreboard.md, which every prompt reads."""
    c.execute("""CREATE TABLE IF NOT EXISTS eng_board (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT,
                 taken_at TEXT, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saved INTEGER,
                 followers INTEGER, score REAL)""")
    plats = ["threads"] + [p for p in ("youtube", "tiktok", "instagram", "facebook")
                           if mm_safety.config()["platforms"].get(p, {}).get("enabled")]
    day = datetime.now().astimezone().weekday() + 1
    lines = [f"## THIS WEEK'S SCOREBOARD (day {day} of 7; only posts made this week count, resets every Monday). "
             "Judge yourself by this week vs last week, not by one post."]
    tot_now = tot_last = 0.0
    names = {"threads": "Threads", "instagram": "Instagram", "youtube": "YouTube Shorts", "tiktok": "TikTok",
             "facebook": "Facebook Reels"}
    for plat in plats:
        cur, last, fol, last_fol = week_results(c, plat)
        c.execute("INSERT INTO eng_board (platform, taken_at, views, likes, comments, shares, saved, followers, score) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (plat, now().isoformat(), cur["views"], cur["likes"], cur["comments"],
                                                 cur["shares"], cur["saved"], _current_followers(c, plat), cur["score"]))
        tot_now += cur["score"]
        tot_last += last["score"]
        line = (f"- {names[plat]}: {cur['posts']} posts this week -> {cur['views']} views, {cur['likes']} likes, "
                f"{cur['comments']} comments, {cur['shares']} shares" + (f", {cur['saved']} saves" if plat == "instagram" else "")
                + f", {fol:+} followers | score {cur['score']:.0f} (last week: {last['score']:.0f} from {last['posts']} posts"
                + (f", {last_fol:+} followers" if last_fol is not None else "") + ")")
        if cur["posts"] == 0:
            line += "  -> nothing posted here this week yet."
        elif last["score"] <= 0:
            line += "  -> " + ("first numbers coming in. Keep going!" if cur["score"] > 0 else "zero engagement. Sad. Change it up.")
        else:
            pct = cur["score"] / last["score"] * 100
            if pct >= 100:
                line += f"  -> ALREADY BEAT LAST WEEK ({pct:.0f}%). Real growth. You're ecstatic."
            elif day >= 5 and pct < 60:
                line += f"  -> only {pct:.0f}% of last week and the week's almost over. Sad. Try something new here."
            else:
                line += f"  -> {pct:.0f}% of last week so far (day {day}/7)."
        if plat == "youtube":
            line += " (uploads are private until the API review passes, so views can't grow yet)"
        lines.append(line)
    for plat in ("youtube", "instagram", "tiktok"):
        bt = mm_safety.best_times_text(c, plat)
        if bt:
            lines.append(f"- Best posting times on {plat} so far: {bt}")
    lines.append(f"- TOTAL this week: {tot_now:.0f} (last week: {tot_last:.0f})")
    scores = {p: c.execute("SELECT score FROM eng_board WHERE platform=? ORDER BY id DESC LIMIT 1", (p,)).fetchone()[0]
              for p in plats}
    if any(scores.values()):
        lines.append(f"- Strongest platform this week: {names[max(scores, key=scores.get)]}")
    c.commit()
    with open(tb.SCOREBOARD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return "\n".join(lines)


def outreach_step(c):
    import mm_outreach
    mm_outreach.tick(c)


def brain_step(c):
    import mm_brain
    mm_brain.tick(c)


def daily_learn(c):
    """Once a day: the bot looks at the whole scoreboard + its best/worst posts on every platform and rewrites
    its memory with what to do differently per platform."""
    row = c.execute("SELECT value FROM meta WHERE key='daily_learn_at'").fetchone()
    if row and now() - datetime.fromisoformat(row[0]) < timedelta(hours=24):
        return
    items = []
    with open(tb.SCOREBOARD_FILE, encoding="utf-8") as f:
        items.append(("DAILY REVIEW OF ALL PLATFORMS:\n" + f.read(), "daily_review", 0.0, []))
    for plat in ("instagram", "youtube"):
        for order in ("DESC", "ASC"):
            r = c.execute(f"SELECT COALESCE(kind,'clip'), title, score FROM mm_posts WHERE platform=? AND score IS NOT NULL "
                          f"AND posted_at > ? ORDER BY score {order} LIMIT 2", (plat, (now() - timedelta(days=7)).isoformat())).fetchall()
            for k, title, sc in r:
                items.append((f"[{plat} {k}] {'BEST' if order == 'DESC' else 'WORST'} this week: {title}", plat, sc or 0.0, []))
    for order in ("DESC", "ASC"):
        for text, kind, sc in c.execute(f"SELECT text, kind, score FROM posts WHERE score IS NOT NULL AND created_at > ? "
                                        f"ORDER BY score {order} LIMIT 2", ((now() - timedelta(days=7)).isoformat(),)).fetchall():
            items.append((f"[threads] {'BEST' if order == 'DESC' else 'WORST'} this week: {text}", kind, sc or 0.0, []))
    import mm_brain
    j = c.execute("SELECT dim, arm, COUNT(*) FROM brain_decisions WHERE at > ? GROUP BY dim, arm ORDER BY dim",
                  ((now() - timedelta(days=1)).isoformat(),)).fetchall()
    if j:
        items.append(("CHOICES YOU MADE TODAY: " + "; ".join(f"{d}={a} x{n}" for d, a, n in j) + "\n" +
                      mm_brain.arm_summary(c), "decisions", 0.0, []))
    tb.reflect(c, items)
    c.execute("INSERT OR REPLACE INTO meta VALUES ('daily_learn_at', ?)", (now().isoformat(),))
    c.commit()


# ---------- learning ----------
def update_stats(c):
    """Pull YouTube stats every 2h, score posts, and teach the shared memory from posts 48h+ old."""
    last = c.execute("SELECT value FROM meta WHERE key='yt_stats_at'").fetchone()
    if last and now() - datetime.fromisoformat(last[0]) < timedelta(hours=2):
        return
    rows = c.execute("SELECT id, remote_id, clip_id, posted_at, learned, real_comments FROM mm_posts "
                     "WHERE platform='youtube'").fetchall()
    if not rows:
        return
    import mm_youtube
    st = mm_youtube.stats([r[1] for r in rows])
    learned = []
    for pid, rid, clip_id, posted_at, already_learned, real in rows:
        m = st.get(rid)
        if not m:
            continue
        s = score(dict(m, comments=m["comments"] if real is None else min(real, m["comments"])))
        c.execute("UPDATE mm_posts SET views=?, likes=?, comments=?, shares=?, retention=?, avg_watch=?, score=?, "
                  "checked_at=? WHERE id=?", (m["views"], m["likes"], m["comments"], m["shares"], m["retention"],
                                              m.get("avg_watch"), s, now().isoformat(), pid))
        age = now() - datetime.fromisoformat(posted_at)
        if not already_learned and age > timedelta(hours=48):  # 48h result -> one lesson for the shared memory
            title = c.execute("SELECT COALESCE(title, '') FROM mm_posts WHERE id=?", (pid,)).fetchone()[0]
            variant = c.execute("SELECT variant FROM mm_clips WHERE id=?", (clip_id,)).fetchone()[0]
            c.execute("UPDATE mm_posts SET learned=1 WHERE id=?", (pid,))
            learned.append((f"[YouTube Short] {title} | style {variant} | {m['views']} views, "
                            f"{m['retention'] or '?'}% viewed", "short_video", s, []))
    # YouTube policy: API data can't sit un-refreshed for more than 30 days. Stats for videos we can no
    # longer refresh (deleted/removed) are wiped once they're 30 days stale.
    cutoff = (now() - timedelta(days=30)).isoformat()
    c.execute("UPDATE mm_posts SET views=NULL, likes=NULL, comments=NULL, shares=NULL, retention=NULL, avg_watch=NULL "
              "WHERE platform='youtube' AND checked_at IS NOT NULL AND checked_at < ?", (cutoff,))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('yt_stats_at', ?)", (now().isoformat(),))
    c.commit()
    if learned:
        tb.reflect(c, learned)


# ---------- production ----------
def produce(c, force=False):
    ok, why = mm_gpu.gpu_allowed()
    if not ok:
        return print(f"Video factory paused: {why}")
    try:
        mm_video.index_channel(c)                 # whole-channel list, refreshed daily
        mm_video.check_new_uploads(c)             # brand-new uploads/streams jump to the front of the line
        mm_video.learn_existing_shorts(c, k=4)    # learn a few of your own Shorts so they're never repeated
        mm_video.keep_sources_flowing(c, tb.ask)  # always keep several different videos/streams loaded + analyzed
    except Exception as e:
        print(f"[library step skipped] {e}")
    ready = c.execute("SELECT COUNT(*) FROM mm_clips WHERE status='ready' AND id NOT IN "
                      "(SELECT clip_id FROM mm_posts GROUP BY clip_id HAVING COUNT(DISTINCT platform)>=2)").fetchone()[0]
    if ready >= mm_safety.config().get("ready_queue", 3) and not force:
        return print(f"Video factory: {ready} clips ready, not making more")
    t = time.time()
    import random
    import mm_images
    cfg = mm_safety.config()
    import mm_brain
    import mm_content
    idea = mm_content.next_idea(c, "video")
    if idea:  # a clip that beat your usual: make more of it
        cid = remix_clip(c, idea[2])
        mm_content.use_idea(c, idea[0])
        if cid:
            return print(f"Video factory: made remix clip {cid} in {time.time() - t:.0f}s")
    cid = mm_video.produce_one(c, tb.ask)
    if cid:
        print(f"Video factory: made clip {cid} in {time.time() - t:.0f}s")


def auto_ai_video(c):
    """Autopilot makes a fully-AI video ONLY when it won't get in your way: no game for 90 minutes
    (ai_quiet_minutes), at most ai_videos_per_day (default 1), and not while 2 are still waiting to be posted."""
    cfg = mm_safety.config()
    if not cfg.get("ai_video", True):
        return
    per_day = int(cfg.get("ai_videos_per_day", 1))
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    made = c.execute("SELECT COUNT(*) FROM mm_clips WHERE ai_visual=1 AND created_at > ?", (since,)).fetchone()[0]
    if made >= per_day:
        return
    waiting = c.execute("SELECT COUNT(*) FROM mm_clips c WHERE c.ai_visual=1 AND c.status='ready' AND NOT EXISTS "
                        "(SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id)").fetchone()[0]
    if waiting >= 2:
        return print(f"AI video: {waiting} are still waiting to be posted, not making another")
    ok, why = mm_gpu.heavy_ok()
    if not ok:
        return print(f"AI video: waiting ({why})")
    import mm_aivideo
    import mm_images
    if not mm_images.comfy_available():
        return print("AI video: ComfyUI isn't available")
    print(f"AI video: {why}, making today's fully-AI video...", flush=True)
    mm_aivideo.make(c, tb.ask, tb.IDENTITY, tb.read_memory(), cfg.get("ollama_model"))


def remix_clip(c, p):
    """A winning clip -> another clip from the same video (best unused moment, same style), or an AI-video sequel."""
    row = c.execute("SELECT c.moment_id, c.variant, COALESCE(c.ai_visual,0), m.video_id, m.text FROM mm_clips c "
                    "JOIN mm_moments m ON m.id=c.moment_id WHERE c.id=?", (p["clip_id"],)).fetchone()
    if not row:
        return None
    mid, variant, ai, vid, text = row
    if ai:
        import mm_aivideo
        print(f"Remix: making an AI-video SEQUEL to \"{p.get('title', '')}\"", flush=True)
        return mm_aivideo.make(c, tb.ask, tb.IDENTITY, tb.read_memory(), mm_safety.config().get("ollama_model"),
                               hint=f"MAKE A SEQUEL to your AI video that {p.get('why', 'did great')}: {text[:400]}. "
                                    "Same characters and idea, new twist.")
    nxt = mm_video._overlaps_used(c, c.execute("SELECT id, '', video_id FROM mm_moments WHERE video_id=? AND used=0 AND "
                                               "ai_score>=6 ORDER BY ai_score DESC LIMIT 10", (vid,)).fetchall())
    nxt = nxt[0] if nxt else None
    if not nxt:
        print("Remix: no more good moments in that video; making a normal clip instead")
        return None
    try:
        v = json.loads(variant or "{}")
    except Exception:
        v = {}
    v = {k: x for k, x in v.items() if k in mm_video.VARIANT_OPTIONS} or mm_video.choose_variant(c)
    if v.get("voice") == "ai_hook":
        import mm_voice
        if not mm_voice.available(c):
            v["voice"] = "none"
    print(f"Remix: another clip from the same video as \"{p.get('title', '')}\" (same style)", flush=True)
    cid = mm_video.render(c, nxt[0], v)
    mm_video.mark_used(c, nxt[0])
    return cid


def update_trends(c):
    import mm_trends
    mm_trends.refresh(c)


HEALTH_FILE = "health.json"


def _health(name, ok, secs, err=None):
    """Remember the last success/failure of every part, for the app's Health tab."""
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            h = json.load(f)
    except Exception:
        h = {}
    e = h.get(name, {})
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if ok:
        e.update(ok_at=stamp, secs=round(secs, 1))
    else:
        e.update(err_at=stamp, err=str(err)[:300])
    e["last"] = "ok" if ok else "error"
    h[name] = e
    with open(HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=1)


def auto_update(c):
    """Once a week: update the YouTube downloader + transcript tool (YouTube breaks them often)."""
    import subprocess
    row = c.execute("SELECT value FROM meta WHERE key='auto_update_at'").fetchone()
    if row and now() - datetime.fromisoformat(row[0]) < timedelta(days=7):
        return
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp", "youtube-transcript-api"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    print("Weekly update of yt-dlp/youtube-transcript-api: " + ("done" if r.returncode == 0 else f"failed: {r.stderr[-300:]}"))
    if r.returncode != 0:
        raise RuntimeError("pip update failed: " + r.stderr[-200:])
    c.execute("INSERT OR REPLACE INTO meta VALUES ('auto_update_at', ?)", (now().isoformat(),))
    c.commit()


def weekly_report(c):
    """Sunday evening: growth per platform, best posts, what it's changing. Saved + pushed to the phone."""
    import mm_notify
    local = datetime.now()
    week = local.strftime("%G-W%V")
    if local.weekday() != 6 or local.hour < 18:
        return
    if (c.execute("SELECT value FROM meta WHERE key='weekly_report'").fetchone() or [""])[0] == week:
        return
    since = (now() - timedelta(days=7)).isoformat()
    lines = [f"CLIP MACHINE WEEKLY REPORT ({local:%b %d})", ""]
    for plat in ("threads", "youtube", "tiktok", "instagram"):
        newest = c.execute("SELECT score, followers FROM eng_board WHERE platform=? ORDER BY id DESC LIMIT 1", (plat,)).fetchone()
        old = c.execute("SELECT score, followers FROM eng_board WHERE platform=? AND taken_at < ? ORDER BY id DESC LIMIT 1",
                        (plat, since)).fetchone()
        if plat == "threads":
            n = c.execute("SELECT COUNT(*) FROM posts WHERE media_id IS NOT NULL AND created_at > ?", (since,)).fetchone()[0]
        else:
            n = c.execute("SELECT COUNT(*) FROM mm_posts WHERE platform=? AND posted_at > ?", (plat, since)).fetchone()[0]
        line = f"{plat.capitalize()}: {n} posts"
        if newest:
            line += f", engagement score {newest[0]:.0f}" + (f" ({newest[0] - old[0]:+.0f} this week)" if old else "")
            line += f", {newest[1]} followers" + (f" ({newest[1] - (old[1] or 0):+})" if old else "")
        lines.append(line)
    best = c.execute("SELECT platform, COALESCE(kind,'clip'), title, score FROM mm_posts WHERE posted_at > ? AND score IS NOT NULL "
                     "ORDER BY score DESC LIMIT 1", (since,)).fetchone()
    tbest = c.execute("SELECT text, score FROM posts WHERE created_at > ? AND score IS NOT NULL ORDER BY score DESC LIMIT 1",
                      (since,)).fetchone()
    if best:
        lines.append(f"\nBest video/picture: [{best[0]} {best[1]}] {best[2]} (score {best[3]:.0f})")
    if tbest:
        lines.append(f"Best Threads post: {tbest[0][:120]} (score {tbest[1]:.0f})")
    mem = tb._memory_only() if os.path.exists(tb.MEMORY_FILE) else ""
    if "## Ideas to try next" in mem:
        ideas = mem.split("## Ideas to try next", 1)[1].split("##", 1)[0].strip()
        lines.append("\nWhat it's trying next:\n" + ideas[:500])
    text = "\n".join(lines)
    with open("weekly_report.md", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    mm_notify.send("Clip Machine weekly report", text, priority=3, tags=["bar_chart"])
    c.execute("INSERT OR REPLACE INTO meta VALUES ('weekly_report', ?)", (week,))
    c.commit()


def _post_count(c):
    try:
        return (c.execute("SELECT COUNT(*) FROM posts WHERE media_id IS NOT NULL").fetchone()[0]
                + c.execute("SELECT COUNT(*) FROM mm_posts").fetchone()[0])
    except Exception:
        return 0


def reply_round(c):
    """Answer everyone who commented/replied, on every platform. Runs right after the machine posts something."""
    for name, fn in (("Threads", lambda: tb.cmd_thank(auto=True)), ("Instagram", lambda: reply_instagram(c)),
                     ("YouTube", lambda: reply_youtube(c)),
                     ("Threads mentions", lambda: __import__("mm_outreach").threads_mentions(c)),
                     ("Instagram DMs", lambda: __import__("mm_dm").tick(c))):
        p = mm_safety.config()["platforms"].get(name.lower().split()[0])
        if p is not None and not p.get("enabled"):
            continue
        try:
            fn()
        except (SystemExit, Exception) as e:
            print(f"[{name} replies skipped] {e}")


def _ensure_reply_task():
    """Make sure the 5-minute 'ClipMachine Replies' task exists (added automatically; the Autopilot OFF switch removes it)."""
    import subprocess
    if os.name != "nt":
        return
    q = subprocess.run(["schtasks", "/query", "/tn", "ClipMachine Replies"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if q.returncode == 0:
        return
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join("windows", "autopilot-setup.ps1")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("Fast reply task: " + ((r.stdout or "").strip().replace("\n", " ") or (r.stderr or "")[-200:]))


def tick():
    started = time.time()
    print(f"\n######## machine tick {datetime.now():%Y-%m-%d %H:%M} ########", flush=True)
    paused, why = mm_gpu.game_pause()
    if paused:  # you're playing: do nothing that touches the GPU, and give back any VRAM the AI was holding
        mm_gpu.free_gpu_now()
        return print(f"Paused: {why}. Everything waits until you close the game.")
    import threading
    import mm_notify
    def _watchdog():  # a stuck run must never block the autopilot forever
        print("[watchdog] this run took over 90 minutes; stopping it so the next one can start", flush=True)
        mm_notify.send("Clip Machine: a run got stuck", "A run took over 90 minutes and was stopped.", priority=4,
                       tags=["warning"])
        try:
            os.remove(LOCK)
        except OSError:
            pass
        os._exit(3)
    wd = threading.Timer(90 * 60, _watchdog)
    wd.daemon = True
    wd.start()
    c0 = con()
    posted_before = _post_count(c0)
    tb.SKIP_REPLIES = True
    t0 = time.time()
    try:
        tb.cmd_auto()  # Threads keeps doing its own thing
        _health("threads", True, time.time() - t0)
    except (SystemExit, Exception) as e:
        print(f"[threads step failed] {e}")
        _health("threads", False, 0, e)
        mm_notify.alert(c0, "Threads", e)
    if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 3 * 3600:
        if _post_count(c0) > posted_before:
            print("Posted something: answering comments now")
            reply_round(c0)
        return print("Machine is still busy from a previous run; skipping video work.")
    open(LOCK, "w").close()
    try:
        c = con()
        steps = (("update", "auto-update", auto_update), ("trends", "trend radar", update_trends),
                 ("stats", "YouTube stats", update_stats),
                 ("tiktok", "TikTok stats", update_tiktok_stats),
                 ("produce", "video factory", produce), ("produce", "AI video (only after 90 min with no games)", auto_ai_video),
                 ("youtube", "YouTube post", post_youtube), ("tiktok", "TikTok post", post_tiktok),
                 ("instagram", "Instagram stats", update_ig_stats),
                 ("instagram", "Instagram post", post_instagram),
                 ("outreach", "outreach (joining other posts + mentions)", outreach_step),
                 ("facebook", "Facebook stats", update_fb_stats), ("facebook", "Facebook post", post_facebook), ("brain", "brain (learn from results)", brain_step),
                 ("scoreboard", "scoreboard", update_scoreboard),
                 ("learn", "daily learning", daily_learn), ("report", "weekly report", weekly_report))
        for plat, label, step in steps:
            p = mm_safety.config()["platforms"].get(plat)
            if p is not None and not p.get("enabled"):
                continue
            t0 = time.time()
            try:
                step(c)
                _health(label, True, time.time() - t0)
            except (SystemExit, Exception) as e:
                print(f"[{label} skipped] {e}")
                _health(label, False, 0, e)
                mm_notify.alert(c, label, e)
            if time.time() - t0 > 20:
                print(f"   ({label} took {time.time() - t0:.0f}s)")
        if _post_count(c) > posted_before:
            print("Posted something this run: answering comments now", flush=True)
            reply_round(c)
        else:
            print("Nothing posted this run: comments wait for the next post")
        waiting = c.execute("SELECT COUNT(*) FROM mm_clips c WHERE status='ready' AND NOT EXISTS "
                            "(SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id)").fetchone()[0]
        left = c.execute("SELECT COUNT(*) FROM mm_moments WHERE used=0 AND ai_score>=6").fetchone()[0]
        print(f"######## run finished in {time.time() - started:.0f}s | clips waiting: {waiting} | "
              f"good moments left: {left} ########", flush=True)
    finally:
        try:
            import mm_images
            mm_images.stop_comfy_if_started()
        except Exception:
            pass
        try:
            os.remove(LOCK)
        except OSError:
            pass


def force_threads():
    """Write and publish one Threads post right now (ignores the once-an-hour timer)."""
    con_ = tb.db()
    feedback = "\n".join(x for x in (tb.account_snapshot(con_), tb.live_post_stats(con_)) if x)
    for _ in range(3):
        candidates, kind, topic = tb.generate_candidates(con_, feedback=feedback)
        safe = [x for x in candidates if tb.safety_check(x)]
        if safe:
            return tb.save_post(con_, safe[0], kind, topic)
    print("Threads: couldn't write a post that passed the safety check. Try again.")


def force(what):
    """Post NOW on one platform (or all), ignoring the daily schedule. Makes a clip first if none is ready."""
    c = con()
    targets = ["threads", "youtube", "tiktok", "instagram", "facebook"] if what == "all" else [what]
    for t in targets:
        print(f"\n--- {t.upper()} ---", flush=True)
        try:
            if t == "threads":
                force_threads()
                continue
            if t == "facebook" and not mm_safety.config()["platforms"].get("facebook", {}).get("enabled"):
                continue
            if t in ("youtube", "tiktok", "facebook") and not next_clip_for(c, t):
                print("No clip ready; making one first (can take a few minutes)...", flush=True)
                produce(c, force=True)
            {"youtube": post_youtube, "tiktok": post_tiktok, "instagram": post_instagram,
             "facebook": post_facebook}[t](c, force=True)
        except (SystemExit, Exception) as e:
            print(f"[{t} failed] {e}")


def threads_about(moment_text, title, angle):
    """Threads post hyping one specific moment/clip (theme mode)."""
    con_ = tb.db()
    raw = tb.ask(f"{tb.IDENTITY}\n\nYOUR MEMORY:\n{tb.read_memory()}\n\nYou just dropped a new clip everywhere: \"{title}\". "
                 f"What happens in it (raw transcript, for context only): {moment_text[:600]}\nAngle: {angle}\n\n"
                 "Write 5 different Threads posts (max 300 chars, no links, no hashtags) that make people curious about "
                 "the clip and want to reply. NEVER paste or quote the transcript; describe the moment in your own words. "
                 "Reply ONLY with a JSON array of 5 strings.")
    import re
    cands = [tb.tidy(x) for x in tb.json_list(raw)]
    tr = set(re.findall(r"[a-z']+", (moment_text or "").lower()))
    cands = [x for x in cands if len(set(re.findall(r"[a-z']+", x.lower())) & tr) < 0.6 * max(1, len(set(
        re.findall(r"[a-z']+", x.lower()))))]  # drop drafts that are mostly the transcript
    if len(cands) > 1:
        import mm_content
        best, improved, _ = mm_content.critique(tb.ask, tb.IDENTITY, "Threads", cands)
        cands = [cands[best]] + [x for i, x in enumerate(cands) if i != best]
    cands = [x for x in cands if x and "http" not in x and not tb.too_morbid(x, "channel")]
    for x in cands:
        if tb.safety_check(x):
            return tb.save_post(con_, x, "channel", None)
    print("Threads: no draft passed the safety check; posting a regular one instead")
    force_threads()


def full_ai_control():
    """One button: the AI decides what to post everywhere RIGHT NOW to chase engagement, then does it."""
    import random
    import re
    import mm_images
    c = con()
    moments = c.execute("SELECT id, title, hook, substr(text,1,220), ai_score FROM mm_moments WHERE used=0 AND ai_score>=6 "
                        "ORDER BY ai_score DESC, RANDOM() LIMIT 8").fetchall()
    comfy = mm_images.comfy_available() and mm_gpu.gpu_allowed()[0]
    stats = ""
    try:
        stats = tb.account_snapshot(tb.db())
    except Exception:
        pass
    import mm_brain
    stats = (stats + "\n" + platform_snapshot(c) + "\n\n" + mm_brain.arm_summary(c)).strip()
    try:
        import mm_trends
        tr = mm_trends.fresh(c, hours=24, limit=6)
        if tr:
            stats += "\n\nTRENDING IN GAMING RIGHT NOW (you can ride one): " + " | ".join(tr)
    except Exception:
        pass
    listing = "\n".join(f"{m[0]}: [{m[4]:.0f}/10] {m[1]} | hook: {m[2]} | {m[3]}" for m in moments) or "(none yet)"
    raw = tb.ask(
        f"{tb.IDENTITY}\n\nYOUR MEMORY:\n{tb.read_memory(scoreboard=True)}\n\n{stats}\n\nYOU HAVE FULL CONTROL. You get to post on Threads, "
        "YouTube Shorts, TikTok and Instagram RIGHT NOW. Goal: the most engagement possible. Pick a plan:\n"
        "- \"theme\": pick ONE moment below; YouTube + TikTok post a clip of it, Instagram posts a reel/meme/carousel of it, "
        "Threads hypes it.\n- \"mixed\": every platform does its own thing (Threads says whatever it wants).\n"
        + ("- \"ai_video\": you dream up a short AI video (cartoon/game-render style, your lore or a dumb joke); it gets "
           "posted to YouTube, TikTok and Instagram and Threads hypes it.\n" if comfy else "") + "\n"
        f"Moments from your videos you could use:\n{listing}\n\nInstagram options: reel, meme, carousel, ai_video"
        + (", ai_image" if comfy else "") + ".\n\nReply ONLY JSON: {\"mode\": \"theme\" or \"mixed\", \"moment_id\": number or null, "
        "\"instagram\": \"...\", \"threads_angle\": \"one line\", \"why\": \"one hype line, in character, about why this will pop off\"}",
        temperature=0.95)
    try:
        plan = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        plan = {}
    ids = [m[0] for m in moments]
    modes = ["theme", "mixed"] + (["ai_video"] if comfy else [])
    mode = plan.get("mode") if plan.get("mode") in modes else random.choice(modes)
    mid = plan.get("moment_id") if plan.get("moment_id") in ids else (random.choice(ids) if ids else None)
    ig = plan.get("instagram") if plan.get("instagram") in IG_KINDS else None
    if ig == "ai_image" and not comfy:
        ig = None
    if mode == "theme" and not mid:
        mode = "mixed"
    print(f"\n=== FULL AI CONTROL: {mode.upper()} ===\nBot: {plan.get('why') or 'watch this.'}\n", flush=True)

    def step(name, fn):
        print(f"--- {name} ---", flush=True)
        try:
            fn()
        except (SystemExit, Exception) as e:
            print(f"[{name} failed] {e}")

    if mode == "ai_video":
        import mm_aivideo
        print("Making an AI video (photo + animation, several minutes)...", flush=True)
        cid = mm_aivideo.make(c, tb.ask, tb.IDENTITY, tb.read_memory(), mm_safety.config().get("ollama_model"))
        if not cid:
            print("AI video didn't work out this time; doing a mixed post instead.")
            mode = "mixed"
        else:
            clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?", (cid,)).fetchone()
            mtext, mtitle = c.execute("SELECT text, title FROM mm_moments WHERE id=?", (clip[5],)).fetchone()
            step("THREADS", lambda: threads_about(mtext, mtitle, plan.get("threads_angle", "") + " (it's an AI video you made)"))
            step("YOUTUBE", lambda: post_youtube(c, force=True, clip=clip))
            step("TIKTOK", lambda: post_tiktok(c, force=True, clip=clip))
            step("INSTAGRAM", lambda: post_instagram(c, force=True, kind="reel", clip=clip))
            print("\nDone. Check back in an hour; the bot will be watching the numbers like a hawk.")
            return
    if mode == "theme":
        clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE moment_id=? AND status='ready' "
                         "ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
        if not clip:
            print("Making the clip for this moment (a few minutes)...", flush=True)
            cid = mm_video.render(c, mid, mm_video.choose_variant(c))
            clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?", (cid,)).fetchone()
        mtext, mtitle = c.execute("SELECT text, title FROM mm_moments WHERE id=?", (mid,)).fetchone()
        step("THREADS", lambda: threads_about(mtext, mtitle, plan.get("threads_angle", "")))
        step("YOUTUBE", lambda: post_youtube(c, force=True, clip=clip))
        step("TIKTOK", lambda: post_tiktok(c, force=True, clip=clip))
        step("INSTAGRAM", lambda: post_instagram(c, force=True, kind=ig or "reel", clip=clip, moment_id=mid))
    else:
        step("THREADS", force_threads)
        for name, fn in (("YOUTUBE", post_youtube), ("TIKTOK", post_tiktok)):
            if not next_clip_for(c, name.lower()):
                print("No clip ready; making one first...", flush=True)
                produce(c, force=True)
            step(name, lambda fn=fn: fn(c, force=True))
        step("INSTAGRAM", lambda: post_instagram(c, force=True, kind=ig))
    print("\nDone. Check back in an hour; the bot will be watching the numbers like a hawk.")


def fetch_more(n):
    """Pull more of the channel in right now: index everything, learn your Shorts, download + analyze n videos."""
    c = con()
    mm_video.index_channel(c, force=True)
    mm_video.learn_existing_shorts(c, k=15)
    got = mm_video.sync_sources(c, downloads=n)
    print(f"Downloaded {got} new source video(s). Finding moments in them (this is the slow part)...", flush=True)
    for (vid,) in c.execute("SELECT video_id FROM mm_sources WHERE analyzed=0").fetchall():
        try:
            print(f"Finding moments in {vid}...", flush=True)
            mm_video.find_moments(c, vid, tb.ask)
        except Exception as e:
            print(f"[analysis failed for {vid}: {e}]")
            c.execute("UPDATE mm_sources SET analyzed=1 WHERE video_id=?", (vid,))
            c.commit()
    good = c.execute("SELECT COUNT(*) FROM mm_moments WHERE used=0 AND ai_score>=6").fetchone()[0]
    print(f"Done. {good} good unused moments are ready to become clips/posts.")


def status():
    c = con()
    ok, why = mm_gpu.gpu_allowed()
    print(f"GPU work: {'ALLOWED' if ok else 'PAUSED'} ({why}) | mode={mm_safety.config().get('gpu_mode')}")
    mm_video.ensure_library(c)
    lib = c.execute("SELECT COUNT(*) FROM mm_library").fetchone()[0]
    used_src = c.execute("SELECT COUNT(*) FROM mm_sources").fetchone()[0]
    shorts_total, shorts_learned = c.execute("SELECT COUNT(*), COALESCE(SUM(checked),0) FROM mm_existing_shorts").fetchone()
    print(f"Channel library: {lib} videos/streams, {used_src} pulled in so far | "
          f"your Shorts learned (never repeated): {shorts_learned}/{shorts_total}")
    print("Good unused moments:", c.execute("SELECT COUNT(*) FROM mm_moments WHERE used=0 AND ai_score>=6").fetchone()[0],
          "| clips made:", c.execute("SELECT COUNT(*) FROM mm_clips").fetchone()[0],
          "| clips waiting:", c.execute("SELECT COUNT(*) FROM mm_clips c WHERE status='ready' AND NOT EXISTS "
                                        "(SELECT 1 FROM mm_posts p WHERE p.clip_id=c.id)").fetchone()[0])
    last = c.execute("SELECT created_at, kind, score, substr(text,1,70) FROM posts WHERE media_id IS NOT NULL "
                     "ORDER BY created_at DESC LIMIT 3").fetchall()
    print("Threads, last 3 posts:")
    for r in last:
        print("   ", r[0][:16], r[1], f"score {r[2]}" if r[2] is not None else "(not scored yet)", "|", r[3])
    try:
        print(update_scoreboard(c))
    except Exception as e:
        print(f"[scoreboard] {e}")
    for plat in ("youtube", "tiktok", "instagram"):
        okp, whyp = mm_safety.may_post(c, plat)
        print(f"{plat}: target {mm_safety.daily_target(c, plat)}/day, posted 24h: {mm_safety.posts_today(c, plat)}, "
              f"can post now: {okp} ({whyp})")
    for r in c.execute("SELECT p.platform, COALESCE(p.kind, 'clip'), COALESCE(p.title, c.title), p.views, p.likes, "
                       "p.retention, p.score, p.tags FROM mm_posts p LEFT JOIN mm_clips c ON c.id=p.clip_id "
                       "ORDER BY p.posted_at DESC LIMIT 10"):
        print("  ", r)


def set_gpu(mode):
    cfg = mm_safety.config()
    cfg["gpu_mode"] = mode
    with open(mm_safety.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"GPU mode set to {mode}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "tick":
        tick()
    elif cmd == "status":
        status()
    elif cmd == "post-clip" and len(sys.argv) >= 3:  # app Queue tab: post one clip everywhere now
        c = con()
        clip = c.execute("SELECT id, path, variant, hook, title, moment_id FROM mm_clips WHERE id=?",
                         (int(sys.argv[2]),)).fetchone()
        if not clip:
            sys.exit("No such clip")
        done = {r[0] for r in c.execute("SELECT platform FROM mm_posts WHERE clip_id=?", (clip[0],))}
        for name, fn, kw in (("youtube", post_youtube, {}), ("tiktok", post_tiktok, {}), ("facebook", post_facebook, {}),
                             ("instagram", post_instagram, {"kind": "reel"})):
            if name in done or not mm_safety.config()["platforms"].get(name, {}).get("enabled"):
                continue
            print(f"\n--- {name.upper()} ---", flush=True)
            try:
                fn(c, force=True, clip=clip, **kw)
            except (SystemExit, Exception) as e:
                print(f"[{name} failed] {e}")
    elif cmd == "fb-check":  # is the Facebook Page ready for Reels?
        __import__("mm_facebook").check()
    elif cmd in ("tiktok-send", "tiktok-recaption", "tiktok-phone"):
        tiktok_command(con(), cmd, int(sys.argv[2]))
    elif cmd == "ntfy-tiktok":  # resend the newest TikTok caption to the phone
        c = con()
        r = c.execute("SELECT id FROM mm_posts WHERE platform='tiktok' ORDER BY id DESC LIMIT 1").fetchone()
        tiktok_command(c, "tiktok-phone", r[0]) if r else print("No TikTok videos yet.")
    elif cmd == "replies":  # by hand only now: answer everyone on every platform (the tick does this right after posting)
        paused, why = mm_gpu.game_pause()
        if paused:
            mm_gpu.free_gpu_now()
            sys.exit(0)  # quietly wait until the game is closed
        print(f"--- replies {datetime.now():%H:%M} ---", flush=True)
        reply_round(con())
    elif cmd == "dm-check":  # can the bot read + answer Instagram DMs?
        __import__("mm_dm").check()
    elif cmd == "dm-now":  # answer Instagram DMs right now
        print(f"{__import__('mm_dm').tick(con())} DM(s) answered")
    elif cmd == "install-music":  # one time: download the AI music model (ACE-Step 1.5, 9.3 GB) into ComfyUI
        __import__("mm_audio").install()
    elif cmd == "make-music":  # make one track now: python machine.py make-music hype
        import mm_audio
        print(mm_audio.generate(sys.argv[2] if len(sys.argv) > 2 else "hype"))
    elif cmd == "brain":  # mood, what's working, decisions, lessons, people it remembers
        import mm_brain
        print(mm_brain.report(con()))
    elif cmd == "force" and len(sys.argv) >= 3:
        force(sys.argv[2])
    elif cmd == "force-picture" and len(sys.argv) >= 3:
        post_instagram(con(), force=True, kind=sys.argv[2])
    elif cmd == "ntfy-test":  # send a test notification to your phone
        import mm_notify
        t = mm_notify.topic()
        ok = mm_notify.send("Clip Machine is connected", "Phone alerts work! You'll get problems, TikTok captions, "
                            "new-upload clips and the Sunday weekly report here.", tags=["tada"])
        print(f"Your ntfy topic: {t}\nIn the ntfy app: + (subscribe) -> topic name: {t} (server ntfy.sh)")
        print("Test notification sent." if ok else "Couldn't reach ntfy.sh.")
    elif cmd == "ai-video":  # make one fully-AI video now: python machine.py ai-video [what it should be about]
        c = con()
        hint = " ".join(sys.argv[2:]).strip() or None
        try:
            update_trends(c)
        except Exception:
            pass
        try:
            cid = __import__("mm_aivideo").make(c, tb.ask, tb.IDENTITY, tb.read_memory(),
                                                mm_safety.config().get("ollama_model"), hint=hint)
        except RuntimeError as e:  # a plain message instead of a wall of traceback
            sys.exit(f"\nCouldn't make the AI video: {e}")
        finally:
            __import__("mm_images").stop_comfy_if_started()
        if cid:
            path = c.execute("SELECT path FROM mm_clips WHERE id=?", (cid,)).fetchone()[0]
            print(f"Saved: {os.path.abspath(path)}\nIt's in the Queue tab; the autopilot posts it on its schedule "
                  "(or press 'Post this clip now' there).")
            if hasattr(os, "startfile"):
                os.startfile(os.path.abspath(path))
    elif cmd == "upload-video" and len(sys.argv) >= 3:  # app: upload your own video everywhere
        import mm_upload
        with open(sys.argv[2], encoding="utf-8") as f:
            job = json.load(f)
        c = con()
        tb.DRY_RUN = False
        clip, secs = mm_upload.prepare(c, job["path"], job.get("title", ""), job.get("caption", ""),
                                       job.get("vertical", True))
        print(f"\nPosting to: {', '.join(job['platforms'])}\n", flush=True)
        lines = mm_upload.post_everywhere(c, clip, secs, job["platforms"])
        print("\nRESULT\n" + "\n".join(lines))
    elif cmd == "index":  # Videos tab: refresh the list of everything on the channel
        mm_video.index_channel(con(), force=True)
    elif cmd == "fetch-picked":  # Videos tab: download + find moments in the videos you starred
        c = con()
        mm_video.ensure_library(c)
        have = {r[0] for r in c.execute("SELECT video_id FROM mm_sources")}
        todo = [r[0] for r in c.execute("SELECT video_id FROM mm_library WHERE priority=2") if r[0] not in have]
        if todo:
            print(f"Downloading {min(len(todo), 5)} starred video(s)...", flush=True)
            mm_video.sync_sources(c, downloads=min(len(todo), 5))
        for vid, title in c.execute("SELECT video_id, title FROM mm_sources WHERE analyzed=0").fetchall():
            try:
                print(f"Finding moments in: {title}", flush=True)
                mm_video.find_moments(c, vid, tb.ask)
            except Exception as e:
                print(f"[analysis failed for {vid}: {e}]")
                c.execute("UPDATE mm_sources SET analyzed=1 WHERE video_id=?", (vid,))
                c.commit()
        print("Done. Starred videos are used first for new clips.")
    elif cmd == "clip-from" and len(sys.argv) >= 3:  # Videos tab: make one clip from this video now
        c = con()
        mm_video.ONLY_VIDEO = sys.argv[2]
        cid = mm_video.produce_one(c, tb.ask)
        if cid:
            path = c.execute("SELECT path FROM mm_clips WHERE id=?", (cid,)).fetchone()[0]
            print(f"Made clip {cid}: {os.path.abspath(path)} (it's in the Queue tab)")
            if hasattr(os, "startfile"):
                os.startfile(os.path.abspath(path))
        else:
            print("No clip made. If this video hasn't been downloaded yet, star it and press 'Download + find "
                  "moments' first.")
    elif cmd == "voice-test":  # say a line in your cloned voice
        import mm_voice
        c = con()
        if not mm_voice.installed():
            print("Voice clone isn't installed yet: run windows\\INSTALL-VOICE.bat (Mac: bash mac/install-voice.sh) first.")
        elif mm_voice.available(c):
            out = os.path.abspath("voice_test.wav")
            mm_voice.speak(" ".join(sys.argv[2:]) or profile.get("sample_voice_line"), out)
            print(f"Saved: {out}")
            if hasattr(os, "startfile"):
                os.startfile(out)
    elif cmd == "ig-check":  # pull Instagram stats + reply to comments right now
        c = con()
        c.execute("DELETE FROM meta WHERE key='ig_stats_at'")
        update_ig_stats(c)
        reply_instagram(c)
        print(platform_snapshot(c) or "No Instagram posts yet.")
    elif cmd == "full-ai":
        full_ai_control()
    elif cmd == "fetch":
        fetch_more(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif cmd == "produce":
        produce(con(), force=True)
    elif cmd == "setup-youtube":
        import mm_youtube
        mm_youtube.credentials(interactive=True)
        cid, name = mm_youtube.whoami()
        want = mm_safety.config()["shorts_channel_id"]
        print(f"Connected to: {name} ({cid})")
        if cid != want:
            print(f"!! That's not the Shorts channel ({want}). Delete youtube_token.json and pick the right channel.")
    elif cmd == "setup-tiktok":
        import mm_tiktok
        mm_tiktok.login()
    elif cmd == "tiktok-status":  # what TikTok did with each upload (inbox draft, failed, posted...)
        import mm_tiktok
        rows = con().execute("SELECT remote_id, posted_at, title FROM mm_posts WHERE platform='tiktok' "
                             "ORDER BY posted_at DESC LIMIT 10").fetchall()
        if not rows:
            print("No TikTok uploads yet.")
        for rid, at, title in rows:
            try:
                st, why = mm_tiktok.status(rid) if rid else ("READY_IN_APP", "waiting in the app's TikTok tab")
            except Exception as e:
                st, why = "ERROR", str(e)
            print(f"{at[:16]}  {st}  {why or ''}  | {title}")
        print("\nSEND_TO_USER_INBOX = it's waiting in your TikTok app (Inbox -> notifications). "
              "FAILED = TikTok rejected it (reason shown).")
    elif cmd == "setup-instagram":
        import mm_instagram
        mm_instagram.setup()
    elif cmd == "test-picture":  # make one Instagram picture locally (no posting) so you can look at it
        import mm_images
        kind = sys.argv[2] if len(sys.argv) > 2 else "meme"
        c = con()
        maker = mm_images.KINDS[kind]
        args = (c, tb.ask, tb.IDENTITY, tb.read_memory())
        item = maker(*args, mm_safety.config().get("ollama_model")) if kind == "ai_image" else maker(*args)
        print(json.dumps(item, indent=2) if item else "Couldn't make one (no unused moments / ComfyUI closed?)")
        if item:
            os.startfile(os.path.abspath(item["paths"][0])) if hasattr(os, "startfile") else None
    elif cmd == "gpu" and len(sys.argv) == 3 and sys.argv[2] in ("auto", "on", "off"):
        set_gpu(sys.argv[2])
    else:
        print(__doc__)
