"""Who this machine works for. Everything personal lives in profile.json (copy profile.example.json and fill it in).
Nothing about a specific creator is hard-coded anywhere else."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_FILE = os.path.join(HERE, "profile.json")
EXAMPLE_FILE = os.path.join(HERE, "profile.example.json")

DEFAULTS = {
    "creator_name": "Creator",             # the name your audience knows you by
    "owner_name": "the owner",             # what the bot calls YOU in the private chat ("Sam")
    "niche": "gaming/comedy",              # one short phrase: what your content is
    "youtube_handle": "",                  # e.g. "@MyChannel"
    "youtube_channel_id": "",              # main channel (source of clips), starts with UC...
    "shorts_channel_id": "",               # channel the Shorts get uploaded to (can be the same)
    "threads_username": "",                # without @
    "instagram_username": "",              # without @
    "facebook_page_url": "",               # https://www.facebook.com/YourPage
    "brand_hashtag": "",                   # e.g. "#MyBrand" (added to every video post), "" = none
    "threads_app_id": "",                  # Meta app > Use cases > Threads API > Settings > Threads app ID
    "lore": [],                            # recurring characters/running jokes, e.g. ["Bob the knight", "the cursed chair"]
    "series_keywords": [],                 # words that mark Shorts worth summarizing (a recurring Shorts series)
    "outreach_topics": ["gaming memes", "PC gaming", "indie games", "gamer humor"],
    "games": [],                           # [["regex", "#Hashtag", "Search Name"], ...] games you play, for tags
    "tag_ideas": ["Gaming", "Gaming Memes", "Gamers", "PC Gaming", "YouTuber", "Memes", "Video Games"],
    "series": [],                          # [["Series name", "what each post in it is"], ...]
    "sample_voice_line": "okay chat, you are NOT ready for this one",
}

_cache = {}


def load():
    if _cache:
        return _cache
    data = dict(DEFAULTS)
    for path in (PROFILE_FILE,):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    # values copied from profile.example.json but never filled in count as blank
    for k in ("youtube_channel_id", "shorts_channel_id"):
        data[k] = channel_id(data.get(k))
    for k in ("youtube_handle", "threads_username", "instagram_username", "facebook_page_url"):
        if _placeholder(data.get(k)):
            data[k] = ""
    data["lore"] = [x for x in data.get("lore") or [] if isinstance(x, str) and not x.lower().startswith("example:")]
    if data.get("creator_name") == "YourName":
        data["creator_name"] = DEFAULTS["creator_name"]
    if data.get("owner_name") == "YourFirstName":
        data["owner_name"] = DEFAULTS["owner_name"]
    _cache.update(data)
    return _cache


def _placeholder(v):
    v = str(v or "")
    return "..." in v or v.lower() in ("@yourchannel", "yourname", "https://www.facebook.com/yourpage")


def channel_id(v):
    """A real YouTube channel ID (UC + 22 characters) or "" (blank, placeholder or a typo)."""
    v = str(v or "").strip()
    return v if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", v) else ""


def handle():
    """@handle of the main channel, or ""."""
    h = str(get("youtube_handle") or "").strip()
    if not h:
        return ""
    h = h.rstrip("/").split("/")[-1]  # a pasted https://www.youtube.com/@Name link works too
    return h if h.startswith("@") else "@" + h


def get(key, default=None):
    v = load().get(key)
    return default if v in (None, "", []) and default is not None else v


def name():
    return get("creator_name", "Creator")


def owner():
    return get("owner_name", "the owner")


def lore_text():
    lore = get("lore") or []
    return ", ".join(lore)


def lore_regex():
    """Regex source matching any lore name (for spotting lore in text), or None when there is no lore."""
    import re
    lore = get("lore") or []
    words = [re.escape(x.lower()) for x in lore if x.strip()]
    return r"\b(" + "|".join(words) + r")\b" if words else None


def fill(text):
    """Put the profile into a prompt: [CREATOR], [OWNER], [NICHE], [LORE], [HANDLE] placeholders."""
    if not isinstance(text, str):
        return text
    lore = lore_text() or "(no recurring characters yet)"
    return (text.replace("[CREATOR]", name()).replace("[OWNER]", owner()).replace("[NICHE]", get("niche", "gaming/comedy"))
            .replace("[LORE]", lore).replace("[HANDLE]", get("threads_username") or get("instagram_username") or name()))
