"""
Hashtags for every platform. Fewer and more specific beats more (researched again Nov 2026):
  - Platforms now read the caption/title WORDS for search, so the game's NAME in the text matters more than any tag.
  - Instagram: hard cap of 5; posts with FEWER tags do better -> 3.
  - TikTok: 3-5; one broad + a few specific ones; #fyp/#viral do nothing -> 4.
  - YouTube Shorts: 2-3 in the description (the first 3 show above the title) -> game, content type, #Shorts.
  - Facebook Reels: 0-3, more looks spammy -> 2.
  - Threads: exactly ONE topic tag per post (threadsbot.pick_tag), no hashtags in the text.
Tags are built from fixed slots (the GAME, the game + "Clips", the kind of content, one broad tag) instead of letting
the AI invent tags nobody searches (#WeaponofWar, #711Bathroom). Every tag's average score is tracked, so the content
slot goes to whatever has actually worked.
"""
import json
import re

import creator_profile as profile

BRAND = (profile.get("brand_hashtag") or "").strip()  # profile.json, "" = no brand tag
PER_PLATFORM = {"youtube": 3, "tiktok": 4, "instagram": 3, "facebook": 2}
BANNED = {"#fyp", "#foryou", "#foryoupage", "#viral", "#viralvideo", "#trending", "#explore", "#explorepage", "#xyzbca",
          "#fypage", "#follow", "#like4like", "#followforfollow", "#tiktok", "#instagram", "#reels", "#youtube"}
CATEGORIES = ["#FunnyMoments", "#GamingMemes", "#GamerHumor", "#FunnyGaming", "#GamingClips", "#GamingFails"]
TYPE_TAGS = {"funny": "#FunnyMoments", "fail": "#GamingFails", "clutch": "#Clutch", "rage": "#GamerRage",
             "chaos": "#FunnyMoments", "weird": "#GamingMemes"}
BROAD = "#Gaming"
# games you play: profile.json "games" first, then common ones: (regex for titles/speech, hashtag, search name)
GAME_LIST = [tuple(g) for g in (profile.get("games") or []) if len(g) == 3] + [
    (r"minecraft", "#Minecraft", "Minecraft"), (r"roblox", "#Roblox", "Roblox"), (r"fortnite", "#Fortnite", "Fortnite"),
    (r"\bgta\b|grand theft auto", "#GTA", "GTA"), (r"pok[eé]mon", "#Pokemon", "Pokemon"),
    (r"garry'?s ?mod|gmod", "#GarrysMod", "Garry's Mod"), (r"terraria", "#Terraria", "Terraria"),
    (r"lethal company", "#LethalCompany", "Lethal Company"), (r"elden ring", "#EldenRing", "Elden Ring"),
    (r"valorant", "#Valorant", "Valorant"), (r"league of legends|\blol\b", "#LeagueOfLegends", "League of Legends"),
    (r"call of duty|warzone", "#CallOfDuty", "Call of Duty"), (r"apex legends", "#ApexLegends", "Apex Legends"),
    (r"overwatch", "#Overwatch2", "Overwatch 2"), (r"counter.?strike|\bcs2\b", "#CS2", "Counter-Strike 2"),
    (r"rocket league", "#RocketLeague", "Rocket League"), (r"stardew", "#StardewValley", "Stardew Valley"),
]
GAMES = {p: t for p, t, _ in GAME_LIST}  # (old name, still used elsewhere)


def _clean(tag):
    tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", (tag or "").lstrip("#"))
    return tag if len(tag) > 2 else None


def tag_stats(con, platform):
    """{tag_lower: (avg score, n)} from this platform's history."""
    stats = {}
    try:
        rows = con.execute("SELECT tags, score FROM mm_posts WHERE platform=? AND score IS NOT NULL AND tags IS NOT NULL",
                           (platform,)).fetchall()
    except Exception:
        return {}
    for tags, score in rows:
        for t in json.loads(tags):
            stats.setdefault(t.lower(), []).append(score)
    return {t: (sum(v) / len(v), len(v)) for t, v in stats.items()}


def best_tags(con, platform, n=6):
    st = tag_stats(con, platform)
    ranked = sorted(((a, t) for t, (a, k) in st.items() if t != BRAND.lower() and k >= 2), reverse=True)
    return [t for _, t in ranked[:n]]


def detect_game(text):
    low = (text or "").lower()
    for pat, tag, _ in GAME_LIST:
        if re.search(pat, low):
            return tag
    return None


def game_name(text):
    """The game's plain name (for putting in the title/caption, which is what search actually reads)."""
    low = (text or "").lower()
    for pat, _, name in GAME_LIST:
        if re.search(pat, low):
            return name
    return None


def _category(con, platform, mtype=None):
    """The content-type tag: what kind of moment it is, or the best-scoring one so far (sometimes another, to keep
    learning)."""
    import random
    if mtype in TYPE_TAGS and random.random() < 0.6:
        return TYPE_TAGS[mtype]
    st = tag_stats(con, platform)
    tried = {c: st[c.lower()][0] for c in CATEGORIES if c.lower() in st and st[c.lower()][1] >= 2}
    if tried and random.random() > 0.3:
        return max(tried, key=tried.get)
    return random.choice(CATEGORIES)


def make_hashtags(con, ask, platform, text, trending=None, mtype=None):
    """Game + game clips + content type + one broad tag, within the platform's (small) limit."""
    total = PER_PLATFORM.get(platform, 3)
    game = detect_game(text)
    if not game:  # a game that's not on the list: ask the AI for its NAME only, and only trust it if it's in the text
        raw = ask(f"Which video game is this about? Text: {text[:700]}\nReply with ONLY the game's exact name, or NONE.",
                  temperature=0.1, max_tokens=20)
        name = re.sub(r"[^A-Za-z0-9' :.-]", "", (raw or "").strip().splitlines()[0] if (raw or "").strip() else "")
        words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2]
        if words and name.upper() != "NONE" and len(name) <= 40 and all(w in (text or "").lower() for w in words):
            game = _clean(name.title().replace(" ", ""))
    cat = _category(con, platform, mtype)
    clips = (game + "Clips") if game and len(game) <= 18 else None
    order = {"youtube": [game, cat, "#Shorts", BROAD],
             "tiktok": [game, clips, cat, BROAD, BRAND],
             "instagram": [game, cat, BROAD, clips],
             "facebook": [game, BROAD, cat]}.get(platform, [game, cat, BROAD])
    import mm_words
    tags = []
    for t in order:
        t = _clean(t) if t else None
        if t and t.lower() not in BANNED and t.lower() not in [x.lower() for x in tags] and mm_words.ok_hashtag(t):
            tags.append(t)
    return tags[:total]


# ---------- per-site text limits ----------
import re as _re

LIMITS = {
    "youtube": {"title": 100, "caption": 5000},   # title: one line, no < or >; description: 5000 bytes, no < or >
    "tiktok": {"title": 2200, "caption": 2200},   # one caption field (title + text + hashtags)
    "instagram": {"title": 2200, "caption": 2200, "hashtags": 5, "mentions": 20},
    "facebook": {"title": 255, "caption": 2200},  # Reels description; 1-3 hashtags work best on Facebook
}


def _cut(text, n):
    """Cut to n chars on a word boundary."""
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return cut if len(cut) > n // 2 else text[:n]


def fit_title(platform, text, fallback=None):
    fallback = fallback or f"{profile.name()} moment"
    t = " ".join((text or "").split())          # no line breaks / double spaces
    if platform == "youtube":
        t = t.replace("<", "").replace(">", "")  # YouTube rejects angle brackets
    t = _cut(t, LIMITS[platform]["title"]).strip()
    return t or fallback


def fit_caption(platform, text):
    t = (text or "").strip()
    if platform == "youtube":
        t = t.replace("<", "").replace(">", "")
        while len(t.encode("utf-8")) > 5000:
            t = t[:-50]
    if platform == "instagram":
        tags = _re.findall(r"#\w+", t)
        for extra in tags[LIMITS["instagram"]["hashtags"]:]:  # keep only the first 5 hashtags
            t = t.replace(extra, "", 1)
        t = _re.sub(r"[ \t]+", " ", t).strip()
    return t[:LIMITS[platform]["caption"]]
