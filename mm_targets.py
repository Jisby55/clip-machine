"""
Where things can be posted: which platforms are connected AND turned on, and the last ones you picked.

  ready(kind)        platforms you can post this kind of thing to right now ("post" = Post now, "video", "image")
  last(kind)         what you ticked last time (only the ones still ready), else everything ready
  remember(kind, ps) save your pick as next time's default

The autopilot's own default is each platform's "enabled" switch in Settings.
"""
import os

import mm_safety

ORDER = ("youtube", "tiktok", "instagram", "facebook", "threads")
NAMES = {"youtube": "YouTube Shorts", "tiktok": "TikTok (gets ready in the TikTok tab)", "instagram": "Instagram",
         "facebook": "Facebook Reels", "threads": "Threads"}
CAN = {"post": ORDER,                                            # Post now: each platform makes its own post
       "video": ("youtube", "tiktok", "instagram", "facebook"),  # one specific video
       "image": ("instagram",)}                                  # one specific picture (the others need video/text)
HOW = {"youtube": "Settings -> Accounts -> Connect YouTube Shorts channel",
       "tiktok": "Settings -> Accounts -> Connect TikTok",
       "instagram": "Settings -> Accounts -> Connect Instagram",
       "facebook": "connect Instagram with a Facebook Page token, then turn Facebook on in Settings",
       "threads": "Settings -> Accounts -> Paste a new Threads token"}


def _ig_connected():
    return os.path.exists("instagram_token.json") or bool(os.environ.get("INSTAGRAM_TOKEN"))


def connected(p):
    if p == "youtube":
        return os.path.exists("youtube_token.json")
    if p == "tiktok":  # until TikTok's audit it only gets the video + caption ready in the app, so no login needed
        return True
    if p in ("instagram", "facebook"):  # Facebook posts with the same Meta (Page) token as Instagram
        return _ig_connected()
    if p == "threads":
        return bool(os.environ.get("THREADS_TOKEN"))
    return False


def enabled(p):
    if p == "threads":  # Threads has no on/off switch; it's on when its token is there
        return True
    try:
        return bool(mm_safety.config()["platforms"].get(p, {}).get("enabled"))
    except Exception:
        return False


def ready(kind="post"):
    return [p for p in CAN.get(kind, ORDER) if enabled(p) and connected(p)]


def missing(kind="post"):
    """[(platform, why)] for the ones that can't be picked right now."""
    out = []
    for p in CAN.get(kind, ORDER):
        if not connected(p):
            out.append((p, f"not connected ({HOW[p]})"))
        elif not enabled(p):
            out.append((p, "turned off in Settings"))
    return out


def last(kind="post"):
    ok = ready(kind)
    try:
        saved = (mm_safety.config().get("post_to_last") or {}).get(kind)
    except Exception:
        saved = None
    picked = [p for p in (saved or []) if p in ok]
    return picked if saved is not None and picked else ok


def remember(kind, platforms):
    try:
        cur = dict(mm_safety.config().get("post_to_last") or {})
        cur[kind] = [p for p in platforms if p in ORDER]
        mm_safety.save_setting("post_to_last", cur)
    except Exception:
        pass


def parse(arg, kind="post"):
    """'youtube,instagram' / 'all' -> list of platform names (all = everything ready)."""
    if not arg or arg == "all":
        return ready(kind)
    return [p for p in (x.strip().lower() for x in arg.split(",")) if p in ORDER]
