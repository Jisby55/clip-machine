"""
Ban-safety rules for the Clip Machine.

Sources (Nov 2026 research):
- YouTube: "inauthentic content" policy targets mass-produced/repetitive uploads, unedited
  prompt-to-upload AI, templated slideshows, other platforms' watermarks. Realistic AI voice/video
  must set status.containsSyntheticMedia. Unaudited API projects upload PRIVATE only.
- TikTok: unaudited apps post SELF_ONLY; inbox uploads max 5 pending per 24h
  (spam_risk_too_many_pending_share); 6 API requests/min per token; is_aigc flag for AI content.
- Instagram: API cap 100 posts/24h but small accounts should stay far below; avoid exact-interval
  posting (add random jitter), never cross-post identical captions, never bot likes/follows/comments,
  set is_ai_generated on AI media.
So: start at 1/day per platform, only ramp up when engagement trends up, random jitter, minimum
spacing, every video gets a unique edit/hook/title, AI labels always on when AI voice/visuals used.
"""
import json
import os
import random
from datetime import datetime, timedelta, timezone

CONFIG_FILE = "machine_config.json"
HARD_CAPS = {"youtube": 3, "tiktok": 4, "instagram": 3, "facebook": 3}  # never exceeded, whatever the config says


def config():
    """machine_config.json (created from machine_config.example.json on first run). Blank channel/account IDs are
    filled in from profile.json, so you only type them once."""
    if not os.path.exists(CONFIG_FILE) and os.path.exists("machine_config.example.json"):
        import shutil
        shutil.copyfile("machine_config.example.json", CONFIG_FILE)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    import creator_profile as profile
    for key, pkey in (("source_channel_id", "youtube_channel_id"), ("shorts_channel_id", "shorts_channel_id")):
        if not cfg.get(key):
            cfg[key] = profile.get(pkey) or (profile.get("youtube_channel_id") if key == "shorts_channel_id" else "") or ""
    return cfg


def _now():
    return datetime.now(timezone.utc)


def posts_today(con, platform):
    since = (_now() - timedelta(hours=24)).isoformat()
    return con.execute("SELECT COUNT(*) FROM mm_posts WHERE platform=? AND posted_at>?",
                       (platform, since)).fetchone()[0]


def last_post_time(con, platform):
    row = con.execute("SELECT MAX(posted_at) FROM mm_posts WHERE platform=?", (platform,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def daily_target(con, platform):
    """Starts at start_per_day. +1 when the last 5 posts beat the 5 before them, -1 when they drop."""
    p = config()["platforms"][platform]
    target = con.execute("SELECT value FROM meta WHERE key=?", (f"target_{platform}",)).fetchone()
    target = int(target[0]) if target else p["start_per_day"]
    today = _now().strftime("%Y-%m-%d")
    adjusted = con.execute("SELECT value FROM meta WHERE key=?", (f"target_day_{platform}",)).fetchone()
    if not adjusted or adjusted[0] != today:  # re-evaluate at most once per day
        scores = [r[0] for r in con.execute(
            "SELECT score FROM mm_posts WHERE platform=? AND score IS NOT NULL ORDER BY posted_at DESC LIMIT 10",
            (platform,))]
        if len(scores) == 10:
            recent, before = sum(scores[:5]) / 5, sum(scores[5:]) / 5
            if recent > before * 1.15:
                target += 1
            elif recent < before * 0.85:
                target -= 1
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"target_day_{platform}", today))
    target = max(1, min(target, p["max_per_day"], HARD_CAPS[platform]))
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"target_{platform}", str(target)))
    con.commit()
    return target


def may_post(con, platform):
    """True if posting now respects the daily target, spacing, jitter and TikTok's pending-inbox cap."""
    cfg = config()
    p = cfg["platforms"].get(platform, {})
    if not p.get("enabled"):
        return False, "disabled"
    if posts_today(con, platform) >= daily_target(con, platform):
        return False, "daily target reached"
    last = last_post_time(con, platform)
    if last:
        spacing = 24 / max(daily_target(con, platform), 1)
        spacing = max(spacing, p["min_hours_between"])
        # random jitter so posts never land on an exact schedule
        key = f"jitter_{platform}_{last.isoformat()}"
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row:
            jitter = int(row[0])
        else:
            jitter = random.randint(0, cfg.get("jitter_minutes", 45))
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(jitter)))
            con.commit()
        if _now() < last + timedelta(hours=spacing, minutes=jitter):
            return False, "too soon"
    ok_time, why_time = good_time(con, platform)
    if not ok_time:
        return False, why_time
    if platform == "tiktok" and not p.get("audited"):
        if posts_today(con, "tiktok") >= 4:  # TikTok allows max 5 pending inbox shares / 24h
            return False, "tiktok inbox limit"
    return True, "ok"


def _local_bin(iso):
    """3-hour slot of the day (0-7) in local time."""
    return datetime.fromisoformat(iso).astimezone().hour // 3


def time_slots(con, platform):
    """{slot: (avg score, n posts)} from this platform's history."""
    out = {}
    for posted_at, score in con.execute("SELECT posted_at, score FROM mm_posts WHERE platform=? AND score IS NOT NULL",
                                        (platform,)):
        b = _local_bin(posted_at)
        out.setdefault(b, []).append(score)
    return {b: (sum(v) / len(v), len(v)) for b, v in out.items()}


def good_time(con, platform):
    """Learned posting times. With 8+ scored posts, only post in 3-hour slots that do at least as well as the
    typical slot; ~25% of weaker or untried slots still get tried each day so it keeps learning."""
    slots = time_slots(con, platform)
    if sum(n for _, n in slots.values()) < 8:
        return True, "ok"
    now_bin = datetime.now().hour // 3
    avgs = sorted(a for a, _ in slots.values())
    median = avgs[len(avgs) // 2]
    if now_bin in slots and slots[now_bin][0] >= median:
        return True, "good time"
    seed = f"{datetime.now():%Y-%m-%d}-{platform}-{now_bin}"
    if random.Random(seed).random() < 0.25:
        return True, "testing this time slot"
    return False, "waiting for a better time of day"


def best_times_text(con, platform):
    slots = time_slots(con, platform)
    if not slots:
        return ""
    top = sorted(slots.items(), key=lambda kv: -kv[1][0])[:2]
    def label(b):
        h1, h2 = b * 3, b * 3 + 3
        f = lambda h: f"{(h % 12) or 12}{'am' if h % 24 < 12 else 'pm'}"
        return f"{f(h1)}-{f(h2)}"
    return ", ".join(f"{label(b)} (avg {a:.0f}, {n} posts)" for b, (a, n) in top)
