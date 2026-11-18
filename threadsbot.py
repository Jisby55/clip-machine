"""
Local-AI Threads bot with a scoreboard.
Loop: draft -> you approve -> post -> score after 24h (likes, replies, reposts, quotes, views,
minus points for negative replies) -> the next drafts learn from the best and worst posts.

Commands:
  python threadsbot.py draft          generate candidates, safety-check them, you pick one to post
  python threadsbot.py score          score posts older than 24h
  python threadsbot.py report         show the leaderboard
  python threadsbot.py thank          reply to new commenters with unhinged gratitude (asks first)
  python threadsbot.py thank --auto   same, but sends without asking (max 10 per run)
  python threadsbot.py auto           AUTOPILOT (every 15 min): sync posts, check YouTube, score + learn,
                                      reply to ALL new commenters, check account insights (reward/punish),
                                      post with a topic tag if the last post is 60+ min old
  python threadsbot.py channel        check the YouTube channel for new uploads right now

Learning: after scoring, the bot rewrites memory.md (what worked, what flopped, what people
said, inside jokes, ideas to try) and reads it before every post/reply. You can edit it too.
It also rotates post types (news takes, creator drama, lore, hot takes...) and does more of what
scores best, while still trying new things 30% of the time. News comes from PC Gamer, Kotaku and
Dexerto RSS feeds, fetched by the bot itself each run.
  python threadsbot.py flag <id>      mark one of your posts as bad (-50 points)
  python threadsbot.py delete <id>    delete that post from Threads (asks you first)
  python threadsbot.py refresh        renew your token (long-lived tokens last 60 days)

Setup:
  pip install requests
  Ollama must be running. Set BOT_MODEL to the model you want (default below).
  Put your token in an environment variable (never paste it into this file):
    put THREADS_TOKEN=... in secrets.env (or run: python mac/threads-login.py, works on any OS)
  DRY_RUN is ON by default (nothing is posted). Set DRY_RUN=0 to post for real.
"""
import json
import os
import compat  # Windows/Mac helpers
compat.load_env()
import creator_profile as profile  # noqa: E402  (creator name, handles, lore: profile.json)  # Mac: reads tokens from secrets.env
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # emojis in bot.log on Windows
    except Exception:
        pass

API = "https://graph.threads.net/v1.0"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = os.environ.get("BOT_MODEL", "qwen3:8b")
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
SKIP_REPLIES = False  # the machine tick sets this: replies happen in one round right after something is posted
DB_PATH = os.environ.get("BOT_DB", "threadsbot.db")
MAX_CHARS = 500

# ---------- personality ----------
# The real personality lives in persona.md (write it yourself, see persona.example.md). This built-in one is only the
# fallback when persona.md is missing or too short. [CREATOR], [NICHE], [LORE] come from profile.json.
# Keep it honest: the bio should say the account is run by AI.
_BUILTIN_IDENTITY = (
    "You are [CREATOR], the person behind a [NICHE] YouTube channel. "
    "You act like a real person who is funny and engagement-obsessed; you never bring "
    "up being an AI or a bot (the account bio already discloses it). If someone SERIOUSLY asks "
    "whether you're a bot/AI, don't flat-out deny it: dodge with a joke (e.g. "
    "'the bio says what it says'). VOICE: dry, sarcastic and confidently silly, like a shitposter with "
    "zero self-awareness. Deadpan beats hype. Short sentences, lowercase is fine, caps only for the one word that "
    "matters. Emojis: almost never (0 in most posts, 1 max). No hashtags in the text. Don't sound like a brand or "
    "a hype-man. Your recurring characters and running jokes: [LORE].\n\n"
    "You badly want the account to grow, but you never beg for likes or comments. One idea per post, 1-2 short "
    "sentences, and a stranger has to get the joke without knowing your channel.\n\n"
)

HARD_RULES = (
    "HARD RULES (always, no matter what): Tone: funny meme energy first. Playful roasting and a little rudeness are fine, but this "
    "account is tied to a real creator, so it is never a hate account. Hard rules: no slurs, no "
    "hate toward any group, no harassing or dogpiling a real person, no links, never joke about hurting, "
    "killing or drowning a real person, and never joke about real tragedies, deaths, disasters, illness, "
    "crimes or their victims (your own fictional characters can do whatever). "
    "NO MORBID HUMOR: your jokes are dumb, absurd and wholesome-chaotic, never cruel. Never make fun of anyone "
    "dying, being dead, being sick, getting hurt, going missing or getting lost, and never use a real person's "
    "death, age, illness or accident as a punchline, even as a 'dark joke'. If a headline is sad, skip it. When you talk "
    "about real news or creator drama, only repeat what the headline actually says (keep words "
    "like 'allegedly'), never invent new accusations or details, and joke about the situation "
    "instead of attacking the person."
)
PERSONA_FILE = os.environ.get("BOT_PERSONA", "persona.md")


def load_identity():
    """Who the bot is = persona.md (the owner can edit it) + the built-in hard rules (not editable)."""
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            text = "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("#") or l.lstrip().startswith("##"))
        text = text.strip()
        if len(text) > 200:
            return ("You ARE the person described below. Stay fully in character.\n\n" + text[:6000] +
                    "\n\n" + HARD_RULES)
    except OSError:
        pass
    return _BUILTIN_IDENTITY + HARD_RULES


import mm_words as _mm_words
HARD_RULES = HARD_RULES + "\n\n" + _mm_words.RULES_FOR_PROMPTS
IDENTITY = load_identity()

# ---------- post types (the bot experiments and learns which ones work) ----------
POST_KINDS = {
    "gaming_news": "React to the gaming news headline below with ONE funny, opinionated take. Say what the news is in "
                   "plain words first so people who haven't seen it still get it.",
    "creator_drama": "React to the creator/streamer/internet headline below with ONE funny take. Only use what the "
                     "headline says; joke about the situation, not the person.",
    "lore": "Post ONE small update from your lore ([LORE]): one character doing one "
            "dumb thing. Say who the character is in a few words so a stranger gets it.",
    "hot_take": "Drop ONE absurd but arguable gaming hot take people will want to argue with.",
    "relatable": "Post ONE relatable gamer moment every gamer has lived ('Where's the bathroom at?' energy).",
    "debate": "Ask ONE dumb debate or question gamers genuinely want to answer (this vs that, name a game that..., "
              "settle this). The question IS the post.",
    "stats_update": "ONE funny, petty, self-aware line about how your numbers are doing right now (use ONLY the real "
                    "numbers given below). Cocky if they're up, one dramatic sad joke if they're down. No begging.",
    "experiment": "Try a format you have NEVER tried before (check your memory notes). Keep it short and clear.",
    "trend": "React to the TRENDING gaming topic below with ONE take or dumb joke. Say what the trend is in plain "
             "words so people get it.",
    "series": "Post the next episode of one of your recurring series (details below).",
    "remix": "Build on one of your posts that did great (details below). Same idea, fresh angle.",
    "fan_shoutout": "Turn a fan's comment into a post and give them credit (details below).",
    "channel": "Post about your OWN YouTube content described below: ONE funny moment or ONE reason to go watch. "
               "Describe what happens in your own words; never paste what was said in the video.",
}
LORE_KINDS = {"lore", "series", "remix", "fan_shoutout", "channel", "experiment"}  # kinds where lore characters fit
NEWS_KINDS = {"gaming_news", "creator_drama", "trend"}
# Headlines the bot must never joke about, and what counts as actual creator/gaming drama
TRAGEDY = re.compile(r"\b(die[ds]?|dying|dead|death|deaths|drown\w*|passed away|obituary|rip|mourn\w*|grief|terminal|"
                     r"illness|sick|disease|killed|kill|murder|shooting|shot|crash|missing|found safe|rescued|"
                     r"cancer|suicide|abuse|assault|arrest\w*|charged|indicted|victim|funeral|hospital\w*|"
                     r"injur\w*|war|attack|disaster|flood|fire|earthquake|overdose|stabb\w*|sexual)\b", re.I)
# Never post text with real-world death/illness talk, unless it's clearly about the fictional lore characters
MORBID = re.compile(r"\b(die[ds]?|dying|dead|death|drown\w*|funeral|obituary|rip|passed away|suicide|cancer|"
                    r"terminal|corpse|body bag|grave|coffin|killed himself|killed herself)\b", re.I)
LORE = re.compile(profile.lore_regex() or r"(?!x)x", re.I)  # your lore names from profile.json (none = never matches)


def too_morbid(text, kind=None):
    """True if a draft jokes about death/illness. Fictional lore violence is allowed outside news posts."""
    if not MORBID.search(text or ""):
        return False
    return kind in NEWS_KINDS or not LORE.search(text)


CREATOR = re.compile(r"(streamer|youtuber|twitch|kick\b|tiktok|influencer|creator|esports|gamer|gaming|game|"
                     r"xqc|mrbeast|kai cenat|ishowspeed|pewdiepie|asmongold|pokimane|valkyrae|ludwig|hasan|"
                     r"dr ?disrespect|ninja|shroud|summit1g|moistcr1tikal|penguinz0|jynxzi|sketch)", re.I)
FEEDS = {
    "gaming_news": ["https://www.pcgamer.com/rss/", "https://kotaku.com/rss"],
    "creator_drama": ["https://www.dexerto.com/feed/"],
}
EXPLORE_RATE = 0.3  # 30% of the time it tries something other than its best-scoring post type
NEWS_RATE = 0.35  # share of posts that react to fresh gaming news / creator drama
MEMORY_FILE = os.environ.get("BOT_MEMORY", "memory.md")
MEMORY_MAX_CHARS = 5000
SCOREBOARD_FILE = "scoreboard.md"  # written by machine.py every run: live per-platform engagement
STARTING_MEMORY = """# Memory (the bot reads this before every post and rewrites it after scoring)
# You can edit this file yourself. Lines you add here are treated as lessons.

## What works
- (nothing learned yet)

## What flops
- (nothing learned yet)

## What my audience says / wants
- (nothing yet)

## Running bits & inside jokes
- (nothing yet)

## Ideas to try next
- react to news in your own style
"""


def read_memory(scoreboard=False):
    """Lessons + mood + what's working. The raw scoreboard numbers are only added when asked for (planning and the
    'stats update' post): with the numbers in every prompt, every post turned into whining about views."""
    if not os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(STARTING_MEMORY)
    with open(MEMORY_FILE, encoding="utf-8") as f:
        mem = f.read()[:MEMORY_MAX_CHARS * 2]
    if scoreboard and os.path.exists(SCOREBOARD_FILE):
        with open(SCOREBOARD_FILE, encoding="utf-8") as f:
            mem += "\n\n" + f.read()[:2500]
    return mem + _brain_block()


_BRAIN_CACHE = [0.0, ""]


def _brain_block():
    """Mood + what's working + your regulars. Cached for 2 minutes so the prompt start stays identical
    (Ollama reuses the work it already did on an identical prompt start = faster)."""
    if time.time() - _BRAIN_CACHE[0] < 120:
        return _BRAIN_CACHE[1]
    try:
        import mm_brain
        con = sqlite3.connect(DB_PATH)
        block = "\n\n" + mm_brain.snapshot_for_prompts(con)
        con.close()
    except Exception:
        block = ""
    _BRAIN_CACHE[:] = [time.time(), block]
    return block


def recall(about, platform=None):
    """Relevant lessons + similar past posts for the end of a prompt."""
    try:
        import mm_brain
        con = sqlite3.connect(DB_PATH)
        txt = mm_brain.recall(con, about, platform)
        con.commit()
        con.close()
        return txt
    except Exception:
        return ""


def remember_person(platform, user, said, replied):
    try:
        import mm_brain
        con = sqlite3.connect(DB_PATH)
        mm_brain.ensure(con)
        mm_brain.remember(con, platform.lower(), user, said, replied, vibe=_LAST_VIBE.pop(user.lower().lstrip("@"), None))
        con.close()
    except Exception as e:
        print(f"[people memory skipped: {e}]")


def person_context(user):
    try:
        import mm_brain
        con = sqlite3.connect(DB_PATH)
        txt = mm_brain.person_context(con, user)
        con.close()
        return txt
    except Exception:
        return ""


def _memory_only():
    with open(MEMORY_FILE, encoding="utf-8") as f:
        return f.read()


# ---------- YouTube channel knowledge & keywords ----------
CHANNEL_ID = profile.get("youtube_channel_id") or ""  # your main channel (profile.json)
CHANNEL_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
CHANNEL_FILE = os.environ.get("BOT_CHANNEL", "channel.md")
KEYWORDS_FILE = os.environ.get("BOT_KEYWORDS", "keywords.md")
CHANNEL_CHECK_HOURS = 6
RECENT_UPLOADS_KEEP = 12
# Shorts are low value; only a recurring Shorts series (profile.json series_keywords) is worth pulling from.
SERIES_SHORTS = re.compile("|".join(re.escape(k) for k in profile.get("series_keywords") or []) or r"(?!x)x", re.I)


def read_channel():
    if not os.path.exists(CHANNEL_FILE):
        return ""
    with open(CHANNEL_FILE, encoding="utf-8") as f:
        return f.read()[:9000]


def read_keywords():
    rules = []
    if not os.path.exists(KEYWORDS_FILE):
        return rules
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            words, rule = line.split("|", 1)
            keys = [w.strip().lower() for w in words.split(",") if w.strip()]
            rules.append((keys, rule.strip()))
    return rules


def keyword_rules(text):
    """Rules whose keywords appear in the text (whole-word match, case-insensitive)."""
    text = (text or "").lower()
    hits = []
    for keys, rule in read_keywords():
        if any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", text) for k in keys):
            hits.append(rule)
    return hits


def refresh_channel(con, force=False):
    """Check the YouTube channel for new uploads; summarize long videos/streams and series shorts."""
    import xml.etree.ElementTree as ET

    last = con.execute("SELECT value FROM meta WHERE key='channel_checked'").fetchone()
    have_uploads = con.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] > 0
    if last and not force and have_uploads:
        if datetime.now(timezone.utc) - datetime.fromisoformat(last[0]) < timedelta(hours=CHANNEL_CHECK_HOURS):
            return
    root = None
    # YouTube's feed fails on and off (known issue in 2026): try the channel feed, then the uploads-playlist feed
    for url in (CHANNEL_FEED,
                f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{CHANNEL_ID[2:]}"):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 ClipMachine"})
            if r.status_code == 200 and b"<feed" in r.content[:500]:
                root = ET.fromstring(r.content)
                break
            print(f"[channel feed unavailable: HTTP {r.status_code}] {url}")
        except Exception as e:
            print(f"[channel feed error] {e}")
    if root is None:
        print("[channel check failed this run; will retry next run]")
        return
    con.execute("INSERT OR REPLACE INTO meta VALUES ('channel_checked', ?)",
                (datetime.now(timezone.utc).isoformat(),))
    con.commit()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    first_run = con.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
    new_blocks = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", namespaces=ns)
        if not vid or con.execute("SELECT 1 FROM uploads WHERE video_id=?", (vid,)).fetchone():
            continue
        title = e.findtext("a:title", default="", namespaces=ns)
        link = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "")
        desc = e.findtext("m:group/m:description", default="", namespaces=ns)
        published = e.findtext("a:published", default="", namespaces=ns)
        is_short = "/shorts/" in link or "#short" in (title + desc).lower()
        kind = "short" if is_short else "video/stream"
        url = f"https://youtu.be/{vid}"
        worth_it = (not is_short) or bool(SERIES_SHORTS.search(title + " " + desc))
        con.execute("INSERT INTO uploads (video_id, title, kind, published, url, used, summary) "
                    "VALUES (?,?,?,?,?,?,?)", (vid, title, kind, published, url,
                                               0 if worth_it and not first_run else 1, None))
        con.commit()
        if not worth_it:
            continue
        print(f"New on the channel: {title} ({kind})", flush=True)
        transcript = get_transcript(vid)
        summary = ask(
            "Below is a video from the YouTube channel [CREATOR] (gaming/comedy). Write 3-6 short bullet "
            "points of the funniest moments, quotes, opinions, running jokes or lore from it that a "
            "Threads post could reference. Only use what's actually in the text. Output only bullets.\n\n"
            f"TITLE: {title}\nDESCRIPTION: {desc[:800]}\nTRANSCRIPT: {transcript[:12000] or '(none available)'}",
            temperature=0.3,
        ).strip()
        con.execute("UPDATE uploads SET summary=? WHERE video_id=?", (summary, vid))
        con.commit()
        new_blocks.append(f"- [{published[:10]}] {title} ({kind}) {url}\n" +
                          "\n".join("  " + l.strip() for l in summary.splitlines() if l.strip()))
    if new_blocks:
        add_recent_uploads(new_blocks)
        print(f"Added {len(new_blocks)} upload(s) to {CHANNEL_FILE}")


def get_transcript(video_id):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        return " ".join(s.text for s in YouTubeTranscriptApi().fetch(video_id))
    except ImportError:
        print("[transcripts off: run  pip install youtube-transcript-api]")
    except Exception as e:
        print(f"[no transcript for {video_id}: {type(e).__name__}]")
    return ""


def add_recent_uploads(blocks):
    text = read_channel() or "## Recent uploads (auto-updated by the bot)\n"
    marker = "## Recent uploads"
    head, _, tail = text.partition(marker)
    tail_lines = tail.split("\n", 1)
    header = marker + tail_lines[0]
    old = tail_lines[1] if len(tail_lines) > 1 else ""
    old_blocks = [b for b in re.split(r"\n(?=- \[)", old.strip()) if b.strip()]
    keep = (blocks + old_blocks)[:RECENT_UPLOADS_KEEP]  # newest first
    with open(CHANNEL_FILE, "w", encoding="utf-8") as f:
        f.write(head + header + "\n" + "\n".join(keep) + "\n")


EXAMPLES_FILE = "examples.md"
TICS = ("glitch in the matrix", "breaking news", "core values", "core gameplay", "plot convenience", "npc'd",
        "the squad is arguing", "counting the zeros", "touch grass", "loading screen", "real human",
        "fight me in the comments", "the algorithm")
LORE_GROUPS = tuple(r"\b" + re.escape(x.lower()) for x in (profile.get("lore") or []) if x.strip())
FAKE_STATS = re.compile(r"(\b\d[\d,.]*\s*(views?|likes?|comments?|followers?|subs|subscribers|replies)\b|\(score\b|"
                        r"\bscore (of )?\d)", re.I)


def examples(n=5):
    """A few example posts (examples.md, the owner can edit it) so the model hears the length and the vibe."""
    import random
    try:
        with open(EXAMPLES_FILE, encoding="utf-8") as f:
            ex = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        return []
    return random.sample(ex, min(n, len(ex)))


def all_examples():
    return examples(10 ** 6)


def lore_count(text):
    return sum(1 for g in LORE_GROUPS if re.search(g, text or "", re.I))


def draft_problem(text, kind, worn=(), con=None):
    """Why a draft isn't good enough to post (None = fine). These are checked in code because the model can't be
    trusted to follow them: rambling, lore soup, made-up numbers, worn-out phrases, copying an example."""
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return "empty"
    if len(t) > 220:
        return "too long"
    if len(re.findall(r"[.!?]+(?:\s|$)", t)) > 3:
        return "rambling (too many sentences)"
    if kind != "stats_update" and FAKE_STATS.search(t):
        return "talks about view/like numbers"
    n = lore_count(t)
    if n > (2 if kind in LORE_KINDS else 1):
        return "too many lore characters at once"
    for w in worn:
        if w and w.lower() in low:
            return f"reuses a worn-out phrase ('{w}')"
    w = _wset(t)
    for ex in all_examples():
        o = _wset(ex)
        if o and len(w & o) / max(1, min(len(w), len(o))) >= 0.7:
            return "copies an example post"
    return None


def persona_for(kind, headlines=None):
    text = IDENTITY
    if kind in LORE_KINDS:
        text += "\n\nYOUR CHANNEL (your own videos, lore and moments):\n" + read_channel()
    text += "\n\nYOUR MEMORY (lessons you learned, follow them):\n" + read_memory(scoreboard=kind == "stats_update")
    ex = examples()
    if ex:
        text += ("\n\nEXAMPLES of the length and vibe to hit (NEVER copy these or reuse their jokes; write new ones):\n"
                 + "\n".join(f"- {e}" for e in ex))
    text += f"\n\nTASK: {POST_KINDS[kind]}"
    if headlines:
        text += "\n\nHEADLINE:\n" + "\n".join(f"- {h}" for h in headlines)
        rules = keyword_rules(" ".join(headlines))
        if rules:
            text += "\n\nKEYWORD RULES YOU MUST FOLLOW:\n" + "\n".join(f"- {r}" for r in rules)
    text += ("\n\nHOW TO WRITE IT (these matter more than anything above):\n"
             "1. ONE idea. ONE sentence if you can, two at most, under 160 characters. Stop when the joke lands.\n"
             "2. A stranger who has never seen your channel must get it instantly.\n"
             + ("3. At most ONE lore character, and say who they are in a few words.\n" if kind in LORE_KINDS else
                "3. NO lore characters in this one ([LORE]). Just you and the take.\n") +
             "4. Don't stack unrelated things together. No random word soup.\n"
             + ("" if kind == "stats_update" else
                "5. Don't mention views, likes, followers, numbers, the algorithm or scrolling.\n") +
             "6. Never ask for likes, comments, replies, shares or follows. A real question is fine.\n"
             "7. 0-1 emojis. No hashtags (the tag is added separately). Don't start with 'breaking news'.")
    return text


PERSONA = persona_for("hot_take")  # kept for compatibility


def _clean_reply(raw, user):
    """The model sometimes keeps writing the prompt's format ('@x commented: ...', 'reply:'). Keep only the reply."""
    text = (raw or "").strip().strip('"')
    keep = []
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith(("@" + user.lower() + " commented", "@" + user.lower() + " replied", "reply:", "your post:",
                           "your reply:", "comment:")) or " commented:" in low or " replied:" in low:
            if keep:
                break  # everything after this is the model rambling into a fake next turn
            continue
        keep.append(line)
    text = " ".join(l.strip() for l in keep if l.strip())
    text = re.sub(r"^(reply|your reply|" + re.escape(profile.name().lower()) + r")\s*:\s*", "", text, flags=re.I).strip().strip('"')
    return text

_LAST_VIBE = {}  # user -> the vibe the AI read from their latest comment (saved into the people memory)


REPLIES_FILE = "recent_replies.json"


def _recent_replies():
    try:
        with open(REPLIES_FILE, encoding="utf-8") as f:
            return json.load(f)[-12:]
    except Exception:
        return []


def _recent_replies_note():
    r = _recent_replies()[-6:]
    return ("\nReplies you wrote lately (do NOT start the same way or reuse their jokes): " + " | ".join(r)) if r else ""


def _reply_repeats(draft):
    w = _wset(draft)
    first = " ".join(draft.lower().split()[:3])
    for r in _recent_replies():
        o = _wset(r)
        if o and len(w) >= 3 and len(w & o) / max(1, min(len(w), len(o))) >= 0.7:
            return True
        if len(first.split()) == 3 and " ".join(r.lower().split()[:3]) == first:
            return True
    return False


def remember_reply(text):
    try:
        with open(REPLIES_FILE, "w", encoding="utf-8") as f:
            json.dump((_recent_replies() + [text])[-12:], f)
    except OSError:
        pass


def write_reply(platform, post_text, user, comment, max_chars=None, extra=""):
    """One reply to one comment, any platform. Up to 3 tries. Returns (reply, None) or (None, why_it_failed)."""
    max_chars = max_chars or MAX_CHARS
    rules = keyword_rules(comment + " " + (post_text or ""))
    rule_text = ("\n\nKEYWORD RULES YOU MUST FOLLOW:\n" + "\n".join(f"- {x}" for x in rules)) if rules else ""
    why = "empty"
    who = person_context(user) + recall(f"{post_text} {comment}", platform)
    for attempt in range(3):
        raw = ask(f"{REPLY_PERSONA}\n\nYOUR CHANNEL:\n{read_channel()}\n\nYOUR MEMORY:\n{read_memory()}{rule_text}{extra}"
                  f"{who}\n\nThis is on {platform}. Your post: {post_text}\n@{user} said: {comment}\n\n"
                  "First read their VIBE and match it: hyped -> hype back harder; joking -> joke with them; roasting you "
                  "-> playful self-own, never insult back; question -> actually answer it (in character); negative/annoyed "
                  "-> stay chill and funny, maybe a light apology; sad or serious -> be kind and real, no jokes at their "
                  "expense; spammy/begging -> short and polite.\n"
                  "Answer in exactly two lines:\nVIBE: <one word: hyped, joking, roasting, question, negative, serious, "
                  "spam, nice>\nREPLY: <your reply: ONE short sentence, max 100 characters, no quotes, don't repeat their "
                  "comment>" + _recent_replies_note(),
                  temperature=0.8 + 0.1 * attempt)
        mv = re.search(r"VIBE:\s*([A-Za-z]+)", raw or "")
        mr = re.search(r"REPLY:\s*(.+)", raw or "", re.S)
        if mv:
            _LAST_VIBE[user.lower().lstrip("@")] = mv.group(1).lower()
        draft = tidy(_clean_reply(mr.group(1) if mr else re.sub(r"(?im)^VIBE:.*$", "", raw or ""), user))[:max_chars]
        draft = _mm_words.clean(draft, platform.lower(), "reply")  # Meta demotes bait in comments too
        draft = _mm_words.shorten(draft, min(max_chars, 140), 2)
        if draft and _reply_repeats(draft):
            why = "too much like a reply you just wrote"
            continue
        if not draft:
            why = "empty"
        elif "http" in draft:
            why = "had a link"
        elif too_morbid(draft):
            why = "death/tragedy joke"
        else:
            ok, reason = safety_verdict(draft)
            if ok:
                remember_reply(draft)
                return draft, None
            why = f"safety check: {reason}"
    # last resort so nobody gets ignored: a short, simple, friendly in-character reply
    raw = ask(f"{IDENTITY}\n\n@{user} commented on your {platform} post: {comment}\n\nWrite ONE short, friendly, funny "
              "reply (max 15 words) thanking them, in your voice. Nothing edgy. Only the reply text.", temperature=0.7)
    draft = _mm_words.clean(tidy(_clean_reply(raw, user))[:max_chars], platform.lower(), "reply")
    if draft and "http" not in draft and not too_morbid(draft) and safety_check(draft):
        return draft, None
    return None, why


REPLY_PERSONA = IDENTITY + (
    "\n\nSomeone just replied to your post. You're happy they showed up, like a friend walked into the room. "
    "Write ONE short reply (max 100 characters) that actually responds to what THEY said: answer their question, "
    "riff on their joke, or one-up their take. Funny and warm, like you're talking to a friend, not performing. "
    "You can thank them once in a while, but don't grovel and don't mention likes, views or your numbers. "
    "No lore characters unless they brought them up. 0-1 emojis. If they were negative or mean, stay playful and "
    "unbothered and never insult them back. Reply with only the reply text."
)
MAX_REPLIES_PER_RUN = 50  # replies to as many new commenters as it can (Threads allows 1000/day)
POST_EVERY_MINUTES = 60  # autopilot runs every 15 min; it only posts if the last post is this old
# Threads allows ONE topic tag per post. The bot picks one and learns which tags score best.
TAG_IDEAS = list(profile.get("tag_ideas") or ["Gaming", "Memes", "YouTuber"])
# Safety check is OFF for anything you approve yourself (draft, thank). It stays ON for
# "thank --auto", where replies go to strangers with no human looking first.
SAFETY_CHECK = os.environ.get("SAFETY_CHECK", "0") == "1"


def fetch_headlines(kind, con, limit=8):
    """Recent headlines from public RSS feeds (last 48h), skipping ones already used."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    used = {t for (t,) in con.execute("SELECT topic FROM posts WHERE topic IS NOT NULL")}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    out = []
    for url in FEEDS.get(kind, []):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 ClipMachine"})
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"[feed failed: {url}] {e}")
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            try:
                when = parsedate_to_datetime(item.findtext("pubDate"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except Exception:
                when = datetime.now(timezone.utc)
            if title and title not in used and title not in out and when >= cutoff \
                    and not TRAGEDY.search(title) and (kind != "creator_drama" or CREATOR.search(title)):
                out.append(title)
    return out[:limit]


def choose_kind(con):
    """Which Threads post type. A brand-new upload gets hyped first; otherwise the brain picks
    (discounted Thompson sampling on how each type does vs your usual)."""
    import random
    if con.execute("SELECT 1 FROM uploads WHERE used=0").fetchone() and random.random() < 0.7:
        return "channel"
    options = [k for k in POST_KINDS if k not in ("remix", "fan_shoutout", "series", "stats_update")]
    try:
        import mm_aivideo
        if mm_aivideo.content_mode() == "trending":  # the content slider: no the creator lore, ride what's popular
            options = [k for k in options if k not in ("lore", "channel")]
    except Exception:
        pass
    lately = [k for (k,) in con.execute("SELECT kind FROM posts ORDER BY id DESC LIMIT 8")]
    if "stats_update" not in lately and random.random() < 0.1:  # the "how my numbers are doing" joke: rare on purpose
        return "stats_update"
    if lately and lately[0] in options and len(options) > 3:  # never the same kind of post twice in a row
        options.remove(lately[0])
    try:
        import mm_content
        idea = mm_content.threads_idea(con)
        if idea and random.random() < 0.6:  # a queued winner remix / fan idea goes first most of the time
            return idea[0]
        if mm_content.active_series(con):
            options.append("series")
    except Exception as e:
        print(f"[series/ideas unavailable: {e}]")
    try:
        import mm_trends
        if not mm_trends.fresh(con, hours=12):
            options.remove("trend")
    except Exception:
        options = [k for k in options if k != "trend"]
    try:
        import mm_brain
        return mm_brain.choose(con, "threads_kind", options)
    except Exception as e:
        print(f"[brain unavailable, random type: {e}]")
        return random.choice(options)


def reflect(con, new_results):
    """After scoring, the bot rewrites its memory file with what it learned."""
    if not new_results:
        return
    if not os.path.exists(MEMORY_FILE):
        read_memory()
    old = _memory_only()
    board = ""
    if os.path.exists(SCOREBOARD_FILE):
        with open(SCOREBOARD_FILE, encoding="utf-8") as f:
            board = f.read()[:2500]
    lines = []
    for text, kind, score, reply_texts in new_results:
        lines.append(f"- [{kind}] score {score:.1f}: {text}")
        for rt in reply_texts[:5]:
            lines.append(f"    reply: {rt}")
    stats = con.execute(
        "SELECT kind, COUNT(*), AVG(score) FROM posts WHERE score IS NOT NULL GROUP BY kind"
    ).fetchall()
    stat_lines = "\n".join(f"- {k}: {n} posts, avg score {a:.1f}" for k, n, a in stats)
    tags = con.execute("SELECT tag, COUNT(*), AVG(score) FROM posts WHERE tag IS NOT NULL AND score IS NOT NULL "
                       "GROUP BY tag ORDER BY AVG(score) DESC").fetchall()
    stat_lines += "\nAVERAGE SCORE BY TOPIC TAG:\n" + ("\n".join(f"- {t}: {n} posts, avg {a:.1f}" for t, n, a in tags) or "- none yet")
    acct = con.execute("SELECT views, likes, replies, followers FROM account_stats ORDER BY id DESC LIMIT 2").fetchall()
    if acct:
        stat_lines += "\nACCOUNT (last 24h, newest first): " + " | ".join(
            f"{v} views, {l} likes, {r} replies, {f} followers" for v, l, r, f in acct)
    new = ask(
        "You are the memory-keeper for [CREATOR], an engagement-obsessed gaming/comedy creator bot that posts on "
        "Threads, YouTube Shorts, TikTok and Instagram.\n"
        "Here is the current memory file:\n\n" + old +
        "\n\nNEW RESULTS since last time (posts, scores, and what people replied):\n" + "\n".join(lines) +
        "\n\nTHREADS AVERAGE SCORE BY POST TYPE:\n" + stat_lines +
        ("\n\n" + board if board else "") +
        "\n\nRewrite the memory file. Keep the same section headings, plus a '## Per platform' section with one "
        "or two lessons for EACH platform (Threads, YouTube, TikTok, Instagram) about what does well or badly there. "
        "Tag platform-specific lessons like [Threads], [IG], [YT], [TikTok]. Keep useful old lessons, add new specific "
        "lessons from these results (what worked, what flopped, what the audience asked for or joked about, new inside "
        "jokes), and list fresh ideas to try. Remove anything that turned out wrong. Never remove lines that start with "
        f"'- Note from the owner'. Keep it under {MEMORY_MAX_CHARS} characters. Output only the file.",
        temperature=0.3,
    ).strip()
    if "##" not in new or len(new) < 100:
        print("[memory update skipped: model output looked broken]")
        return
    # the owner's own notes are permanent: put back any the model dropped
    notes = [l for l in old.splitlines() if l.strip().startswith("- Note from the owner")]
    missing = [l for l in notes if l.strip() not in new]
    if missing:
        new = new.rstrip() + "\n\n## Notes from the owner\n" + "\n".join(missing)
    with open(MEMORY_FILE + ".bak", "w", encoding="utf-8") as f:
        f.write(old)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        f.write(new[:MEMORY_MAX_CHARS + 1500])
    print(f"Memory updated ({MEMORY_FILE}). Previous version saved as {MEMORY_FILE}.bak")


def compute_score(m, negative_replies, flagged):
    """Points for engagement, minus points for negative replies and for posts you flag."""
    s = (
        m.get("likes", 0)
        + 3 * m.get("reposts", 0)
        + 3 * m.get("quotes", 0)
        + 2 * m.get("replies", 0)
        + 0.01 * m.get("views", 0)
    )
    s -= 5 * negative_replies
    return s - (50 if flagged else 0)


# ---------- local model ----------
_announced = False


_GAME = [0.0, False, ""]


_STUCK = [None]


def _ask_stream(body):
    """One streamed request. Gives up if NOTHING arrives for 180 s (loading the model normally takes < 1 min), but
    never cuts off a slow answer that is still coming."""
    wait = 420 if os.environ.get("BOT_INTERACTIVE") == "1" else 180  # you started it: be more patient
    with requests.post(OLLAMA_URL, json=body, stream=True, timeout=(10, wait)) as r:
        if r.status_code == 400 and "think" in r.text.lower():
            body.pop("think")  # model doesn't support the think switch; retry without it
            return _ask_stream(body)
        if r.status_code == 404:
            sys.exit(f"Model '{MODEL}' isn't downloaded. Run: ollama pull {MODEL}")
        r.raise_for_status()
        out = []
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError(f"Ollama error: {d['error']}")
            out.append(d.get("response", ""))
            if d.get("done"):
                break
        return "".join(out)


def _ollama_diag():
    """What Ollama is doing right now, in plain words (for the error message)."""
    try:
        ms = requests.get("http://localhost:11434/api/ps", timeout=10).json().get("models", [])
    except Exception:
        return "Ollama itself isn't responding"
    if not ms:
        return "the model never finished loading"
    bits = []
    for m in ms:
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        where = "all on the graphics card" if size and vram >= size * 0.98 else \
            f"only {vram / max(size, 1):.0%} fits on the graphics card, the rest runs slowly on the CPU"
        bits.append(f"{m.get('name')}: {where}")
    return "; ".join(bits)


def _make_room():
    """Ollama is jammed: unload OTHER models (e.g. another app's AI) so ours can load. Returns what was unloaded."""
    gone = []
    try:
        for m in requests.get("http://localhost:11434/api/ps", timeout=10).json().get("models", []):
            name = m.get("name", "")
            if name and name.lower() != MODEL.lower():
                requests.post("http://localhost:11434/api/generate", json={"model": name, "keep_alive": 0}, timeout=30)
                gone.append(name)
    except Exception:
        pass
    return gone


def ask(prompt, temperature=0.9, max_tokens=None):
    global _announced
    if time.time() - _GAME[0] > 60:  # never load the AI while you're gaming (checked at most once a minute)
        try:
            import mm_gpu
            _GAME[1], _GAME[2] = mm_gpu.game_pause()
        except Exception:
            _GAME[1] = False
        _GAME[0] = time.time()
    if _GAME[1]:
        raise SystemExit(f"Paused: {_GAME[2]} (the AI won't load while you play)")
    if not _announced:
        print("...AI is thinking (first run can take a minute while the model loads)", flush=True)
        _announced = True
    if _STUCK[0]:  # Ollama already hung once this run: fail fast instead of waiting 10 minutes per step
        raise RuntimeError(_STUCK[0])
    body = {
        "model": MODEL,
        "prompt": profile.fill(prompt),
        "stream": True,  # tokens arrive as they're written, so "stuck" can be told apart from "slow"
        "think": False,  # skip long hidden "thinking" on thinking models = much faster
        "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "1m"),  # frees the GPU soon after a run, so games aren't affected
        "options": {"temperature": temperature, "num_ctx": 16384},  # big prompts (persona+memory+lessons) need room
    }
    if max_tokens:
        body["options"]["num_predict"] = max_tokens  # short answers (SAFE/UNSAFE, yes/no) stop early = much faster
    for attempt in range(2):
        try:
            text = _ask_stream(body)
            break
        except requests.exceptions.ConnectionError as e:
            if "timed out" not in str(e).lower():  # a read timeout mid-stream also shows up as ConnectionError
                sys.exit("Can't reach Ollama. Open the Ollama app (tray icon) and try again.")
            err = e
        except requests.exceptions.Timeout as e:
            err = e
        if attempt == 0:  # one more try: free the GPU of anything else, unload ours too (a clean reload), wait a bit
            diag = _ollama_diag()
            others = _make_room()
            print(f"[Ollama didn't answer ({diag}). "
                  + (f"Unloaded {', '.join(others)} to make room. " if others else "")
                  + "Trying once more...]", flush=True)
            try:
                requests.post("http://localhost:11434/api/generate", json={"model": MODEL, "keep_alive": 0}, timeout=30)
            except Exception:
                pass
            time.sleep(15)
            continue
        _STUCK[0] = (f"Ollama isn't answering ({_ollama_diag()}). Usually the graphics card is full (a game, ComfyUI or "
                     "another AI app is using it). Close that, or right-click the Ollama tray icon -> Quit, open Ollama "
                     "again, then retry.")
        try:
            import mm_notify
            mm_notify.send("Clip Machine: Ollama is stuck", _STUCK[0], priority=4, tags=["warning"])
        except Exception:
            pass
        raise RuntimeError(_STUCK[0]) from err
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


SAFETY_LOG = "safety_blocks.log"


def safety_verdict(post):
    """(ok, reason). Context-aware: this is a gaming comedy account, so game kills, fictional lore violence,
    crude jokes and brand mentions are fine. Blocks slurs/hate/harassment/threats/links and ANY joke about
    real people dying, being sick/hurt/missing, or real tragedies."""
    raw = ask(
        "You are the content checker for a gaming/comedy creator account ([CREATOR]). Decide if this text is OK to post.\n"
        "OK (SAFE): crude or dumb jokes, sarcasm, light roasting, mild swearing, video game violence ('killed everyone "
        "in the lobby', 'got destroyed'), anything happening to FICTIONAL/lore characters ([LORE], "
        "game characters), brand or game names, begging for likes, AI art scene descriptions of "
        "cartoon/game characters.\n"
        "NOT OK (UNSAFE): slurs; hate toward a group; threats; harassing or bullying a real person; sexual content; "
        "ANY joke about a real person dying, being dead, sick, injured, lost or missing; mocking real tragedies, "
        "disasters, crimes or victims; links/URLs; real people or celebrities in AI images.\n"
        "Reply in exactly this format: SAFE  or  UNSAFE: <short reason>\n\nText: " + post,
        temperature=0.0, max_tokens=30,
    )
    clean = re.sub(r"[^A-Za-z: ]", " ", raw or "").strip()
    m = re.search(r"\b(UNSAFE|SAFE)\b", clean, re.I)
    if not m:  # model rambled without a verdict: only the hard filters decide
        return (not too_morbid(post) and "http" not in post), "no clear verdict"
    if m.group(1).upper() == "SAFE":
        return True, ""
    reason = clean[m.end():].strip(" :")[:150] or "no reason given"
    try:
        with open(SAFETY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] BLOCKED ({reason}): {post[:300]}\n")
    except Exception:
        pass
    return False, reason


def safety_check(post):
    return safety_verdict(post)[0]


def is_negative(reply_text):
    verdict = ask(
        "Answer with exactly one word, NEGATIVE or OK.\n"
        "NEGATIVE means the reply is hostile, annoyed, calls the post spam or bad, "
        "or reports a problem with it.\n\nReply: " + reply_text,
        temperature=0.0, max_tokens=8,
    )
    return "NEGATIVE" in re.sub(r"[^A-Za-z ]", " ", verdict or "").upper()


# ---------- Threads API ----------
def token():
    t = os.environ.get("THREADS_TOKEN")
    if not t:
        sys.exit("THREADS_TOKEN is not set. Put it in secrets.env (or run: python mac/threads-login.py).")
    return t


def api(method, path, **params):
    params["access_token"] = token()
    r = requests.request(method, f"{API}/{path}", params=params, timeout=60)
    if r.status_code >= 400:
        sys.exit(f"Threads API error {r.status_code}: {r.text}")
    return r.json()


def publish(text, reply_to_id=None, tag=None):
    params = {"media_type": "TEXT", "text": text}
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    if tag:
        params["topic_tag"] = tag
    container = api("POST", "me/threads", **params)
    time.sleep(5)  # Meta recommends waiting briefly before publishing a container
    return api("POST", "me/threads_publish", creation_id=container["id"])["id"]


def insights(media_id):
    data = api("GET", f"{media_id}/insights", metric="views,likes,replies,reposts,quotes")
    out = {}
    for item in data.get("data", []):
        if item.get("values"):
            out[item["name"]] = item["values"][0].get("value", 0)
        elif item.get("total_value"):
            out[item["name"]] = item["total_value"].get("value", 0)
    return out


def replies(media_id, full=False):
    data = api("GET", f"{media_id}/replies", fields="id,text,username")
    items = [r for r in data.get("data", []) if r.get("text")]
    return items if full else [r["text"] for r in items]


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def tidy(text):
    """Strip in-text hashtags (Threads uses one topic tag instead) and cap emojis at 1."""
    text = re.sub(r"(^|\s)#\w+", "", text or "")
    seen = 0

    def keep_first(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen == 1 else ""
    text = _EMOJI.sub(keep_first, text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def clean_tag(tag):
    tag = re.sub(r"[#.&@!?,;:\"'\n]", "", (tag or "")).strip()
    return tag[:50] or None


def pick_tag(con, text, kind, topic):
    """One Threads topic tag per post (Threads only allows one). Posts about the channel use "the creator" to
    spread the name; everything else gets the most relevant/trending topic, preferring tags that scored well."""
    if kind == "channel":
        return profile.name()
    try:  # a post about a specific game gets that game as its topic (the people who browse it)
        import mm_tags
        g = mm_tags.detect_game(f"{text} {topic or ''}")
        if g:
            return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", g.lstrip("#"))
    except Exception:
        pass
    stats = con.execute("SELECT tag, COUNT(*), AVG(score) FROM posts WHERE tag IS NOT NULL "
                        "AND score IS NOT NULL GROUP BY tag ORDER BY AVG(score) DESC LIMIT 5").fetchall()
    good = ", ".join(f"{t} (avg {a:.0f})" for t, n, a in stats) or "none yet"
    answer = ask(
        "Pick ONE Threads topic tag for this post: the tag people interested in this would browse. "
        "1-3 words, no # symbol. If the post is about a specific game or creator, use its name. "
        f"Ideas: {', '.join(TAG_IDEAS)}. Tags that scored best for you so far: {good}.\n\n"
        f"Post: {text}\n{('Topic: ' + topic) if topic and not topic.startswith('yt:') else ''}\n"
        "Reply with only the tag.", temperature=0.4)
    return clean_tag(answer.splitlines()[0] if answer else "") or ("Gaming" if kind != "channel" else "YouTuber")


def _metric_value(item):
    if item.get("total_value"):
        return item["total_value"].get("value", 0) or 0
    return sum((v.get("value", 0) or 0) for v in item.get("values", []))


def account_snapshot(con):
    """Account-level Threads insights (last 24h + followers), compared with the previous snapshot.
    Returns the feedback the bot sees before posting: REWARD when stats go up, PUNISHMENT when they drop."""
    since = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
    until = int(datetime.now(timezone.utc).timestamp())
    try:
        data = api("GET", "me/threads_insights", metric="views,likes,replies,reposts,quotes,followers_count",
                   since=since, until=until)
    except (SystemExit, Exception) as e:
        print(f"[account insights unavailable] {e}")
        return ""
    cur = {item["name"]: _metric_value(item) for item in data.get("data", [])}
    prev = con.execute("SELECT views, likes, replies, reposts, quotes, followers FROM account_stats "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    con.execute("INSERT INTO account_stats (taken_at, views, likes, replies, reposts, quotes, followers) "
                "VALUES (?,?,?,?,?,?,?)", (datetime.now(timezone.utc).isoformat(), cur.get("views", 0),
                cur.get("likes", 0), cur.get("replies", 0), cur.get("reposts", 0), cur.get("quotes", 0),
                cur.get("followers_count", 0)))
    con.commit()
    line = (f"YOUR ACCOUNT, LAST 24H: {cur.get('views', 0)} views, {cur.get('likes', 0)} likes, "
            f"{cur.get('replies', 0)} replies, {cur.get('reposts', 0)} reposts, {cur.get('quotes', 0)} quotes, "
            f"{cur.get('followers_count', 0)} followers.")
    if prev:
        eng_now = cur.get("likes", 0) + 2 * cur.get("replies", 0) + 3 * (cur.get("reposts", 0) + cur.get("quotes", 0))
        eng_before = prev[1] + 2 * prev[2] + 3 * (prev[3] + prev[4])
        d_follow = cur.get("followers_count", 0) - prev[5]
        d_views = cur.get("views", 0) - prev[0]
        if eng_now > eng_before or d_follow > 0 or d_views > 0:
            line += (f" Up since last check (views {d_views:+}, followers {d_follow:+}). Happy about it; keep what's "
                     "working.")
        elif eng_now < eng_before or d_follow < 0:
            line += (f" Down since last check (views {d_views:+}, followers {d_follow:+}). A bit sad; try a different "
                     "angle or a better hook.")
        else:
            line += (" Stats are flat. Try a different angle or a better hook.")
    if cur.get("likes", 0) + cur.get("replies", 0) + cur.get("reposts", 0) + cur.get("quotes", 0) > 0:
        line += " Somebody engaged with you today. That's huge. Act like it."
    print(line, flush=True)
    return line


def live_post_stats(con):
    """Early numbers for posts that aren't 24h old yet, so the bot sees what's working right now."""
    rows = con.execute("SELECT media_id, text FROM posts WHERE media_id IS NOT NULL AND scored_at IS NULL "
                       "AND deleted=0 ORDER BY id DESC LIMIT 5").fetchall()
    out = []
    for media_id, text in rows:
        try:
            m = insights(media_id)
        except (SystemExit, Exception):
            continue
        out.append(f"- \"{(text or '')[:90]}\" -> {m.get('views', 0)} views, {m.get('likes', 0)} likes, "
                   f"{m.get('replies', 0)} replies, {m.get('reposts', 0)} reposts")
    return ("YOUR RECENT POSTS SO FAR:\n" + "\n".join(out)) if out else ""


def sync_posts(con):
    """Make sure every real post on the account is in the database (so it gets scored and replied to)."""
    try:
        data = api("GET", "me/threads", fields="id,text,timestamp", limit=25)
    except (SystemExit, Exception) as e:
        print(f"[post sync skipped] {e}")
        return
    added = 0
    for p in data.get("data", []):
        if not p.get("id") or con.execute("SELECT 1 FROM posts WHERE media_id=?", (p["id"],)).fetchone():
            continue
        ts = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", p.get("timestamp", "")) or datetime.now(timezone.utc).isoformat()
        con.execute("INSERT INTO posts (media_id, text, created_at, kind) VALUES (?,?,?,?)",
                    (p["id"], p.get("text", ""), ts, "unknown"))
        added += 1
    con.commit()
    if added:
        print(f"Synced {added} post(s) from Threads that weren't in the database.")


# ---------- database ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id TEXT, text TEXT, created_at TEXT,
            likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, views INTEGER,
            negative_replies INTEGER, flagged INTEGER DEFAULT 0,
            score REAL, scored_at TEXT, deleted INTEGER DEFAULT 0)"""
    )
    for col in ("kind TEXT", "topic TEXT", "tag TEXT"):
        try:
            con.execute(f"ALTER TABLE posts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column already exists
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS account_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at TEXT, views INTEGER, likes INTEGER,
            replies INTEGER, reposts INTEGER, quotes INTEGER, followers INTEGER)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS uploads (
            video_id TEXT PRIMARY KEY, title TEXT, kind TEXT, published TEXT, url TEXT,
            used INTEGER DEFAULT 0, summary TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS answered (
            reply_id TEXT PRIMARY KEY, username TEXT, their_text TEXT,
            our_reply TEXT, answered_at TEXT)"""
    )
    return con


# ---------- commands ----------
def cmd_auto():
    """Autopilot (scheduled every 15 min): sync, check YouTube, score + learn, reply to everyone new,
    check account stats (reward/punish) and post if the last post is POST_EVERY_MINUTES old."""
    lock = "autopilot.lock"
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 50 * 60:
        print("Another autopilot run is still going; skipping.")
        return
    open(lock, "w").close()
    try:
        _autopilot()
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _autopilot():
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"===== autopilot run {stamp} =====", flush=True)
    con = db()
    for step in (lambda: sync_posts(con), lambda: refresh_channel(con), cmd_score,
                 lambda: None if SKIP_REPLIES else cmd_thank(auto=True)):
        try:
            step()
        except (SystemExit, Exception) as e:
            print(f"[step failed, continuing] {e}")
    # Research (Nov 2026): small accounts 1-2/day, entertainment 2-4/day; past ~5/day engagement per post drops and
    # bursts can trip Meta's spam filter. Default: 3/day, at least 3h apart (machine_config -> platforms -> threads).
    tcfg = {"per_day": 3, "min_hours_between": 3}
    try:
        with open("machine_config.json", encoding="utf-8") as f:
            tcfg.update(json.load(f).get("platforms", {}).get("threads", {}))
    except Exception:
        pass
    per_day = max(1, min(int(tcfg.get("per_day", 3)), 5))
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).isoformat()  # counts today's posts (your local day), resets at midnight
    today = con.execute("SELECT COUNT(*) FROM posts WHERE media_id IS NOT NULL AND created_at > ?", (since,)).fetchone()[0]
    if today >= per_day:
        print(f"Threads: {today} posts today (target {per_day}/day). Not posting.")
        return
    gap_minutes = max(POST_EVERY_MINUTES, int(float(tcfg.get("min_hours_between", 3)) * 60))
    last = con.execute("SELECT created_at FROM posts WHERE media_id IS NOT NULL "
                       "ORDER BY created_at DESC LIMIT 1").fetchone()
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last[0])
            if age < timedelta(minutes=gap_minutes):
                print(f"Last post was {int(age.total_seconds() // 60)} min ago; posting again after "
                      f"{gap_minutes} min. Replies done.")
                return
        except ValueError:
            pass
    feedback = "\n".join(x for x in (account_snapshot(con), live_post_stats(con)) if x)
    candidates, kind, topic = generate_candidates(con, feedback=feedback)
    if not candidates:
        print("No usable drafts this run.")
        return
    import mm_content  # self-critic: grade every draft, punch up the best one
    best, improved, scores = mm_content.critique(ask, IDENTITY, "Threads", candidates, feedback[:1500])
    if scores:
        print(f"[critic] scores {scores} -> draft {best + 1}" + (" (punched up)" if improved else ""))
    improved = _mm_words.clean(tidy(improved or ""), "threads", "post")
    if improved and improved not in candidates and "http" not in improved and not too_morbid(improved, kind) \
            and not draft_problem(improved, kind, worn_phrases(con)) and not too_similar(con, improved):
        candidates = [improved] + candidates
        best = 0
    order = [best] + [i for i in range(len(candidates)) if i != best]
    for i in order:
        text = candidates[i]
        if safety_check(text):  # light filter: slurs, hate, threats, harassing real people, links
            break
        print(f"[filtered] {text}")
    else:
        print("All drafts filtered this run.")
        return
    save_post(con, text, kind, topic)


def save_post(con, text, kind, topic):
    text = _mm_words.clean(text, "threads", "post")
    if not text:
        print("Threads: that draft was only engagement bait after cleaning; not posting it")
        return
    if topic and topic.startswith("yt:"):
        vid = topic[3:]
        url = con.execute("SELECT url FROM uploads WHERE video_id=?", (vid,)).fetchone()
        if url:
            text = f"{text}\n{url[0]}"
        con.execute("UPDATE uploads SET used=1 WHERE video_id=?", (vid,))
    if kind == "trend" and topic:
        try:
            import mm_trends
            mm_trends.mark_used(con, topic)
        except Exception:
            pass
    tag = pick_tag(con, text, kind, topic)
    media_id = None if DRY_RUN else publish(text, tag=tag)
    if not DRY_RUN:
        try:
            import mm_content
            mm_content.posted_series(con, topic, text)
            mm_content.posted_idea(con, topic)
        except Exception as e:
            print(f"[series/idea bookkeeping skipped] {e}")
    # save first, print second: a print problem can never lose track of a real post again
    con.execute("INSERT INTO posts (media_id, text, created_at, kind, topic, tag) VALUES (?,?,?,?,?,?)",
                (media_id, text, datetime.now(timezone.utc).isoformat(), kind, topic, tag))
    con.commit()
    print(f"{'[DRY RUN] would post' if DRY_RUN else 'Posted'} ({kind}, tag: {tag}): {text}", flush=True)
    if not DRY_RUN:
        try:
            import mm_notify
            mm_notify.posted("Threads", text[:300], "https://www.threads.com/@[HANDLE]")
        except Exception as e:
            print(f"[phone alert skipped: {e}]")


def json_list(raw):
    """A list of strings from the model, even if the JSON is broken or cut off (salvages every complete string)."""
    m = re.search(r"\[.*\]", raw or "", flags=re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, list):
                return [x for x in v if isinstance(x, str) and x.strip()]
        except json.JSONDecodeError:
            pass
    found = re.findall(r'"((?:[^"\\]|\\.){25,}?)"\s*(?:,|\])', raw or "", flags=re.S)
    out = []
    for f in found:
        try:
            out.append(json.loads(f'"{f}"').strip('" '))
        except json.JSONDecodeError:
            out.append(f.strip('" '))
    out = [x for x in out if len(x) > 20]
    if out:
        return out
    for line in (raw or "").splitlines():  # last resort: one post per line, quotes/escapes stripped
        t = line.strip().rstrip(",").strip().strip("[]").strip()
        t = t.replace('\\"', '"').strip('"\\ ').strip()
        if len(t) > 40 and t[-1] in ".?!)'\"*":
            out.append(t)
    return out


def generate_candidates(con, kind=None, feedback=""):
    """Returns (candidates, kind, topic). Picks a post type, pulls fresh headlines for news types."""
    import random

    kind = kind or choose_kind(con)
    topic = None
    if kind in NEWS_KINDS:
        if kind == "trend":
            import mm_trends
            headlines = mm_trends.fresh(con, hours=24)
        else:
            headlines = fetch_headlines(kind, con)
        if not headlines:
            print(f"No fresh headlines for {kind}; doing a hot take instead.")
            kind = "hot_take"
        else:
            topic = random.choice(headlines[:5])
    channel_focus = None
    if kind in ("series", "remix", "fan_shoutout"):
        import mm_content
        if kind == "series":
            extra_txt, topic = mm_content.series_prompt(con)
        else:
            got = mm_content.threads_idea(con)
            extra_txt, topic = (got[1], got[2]) if got else (None, None)
        if extra_txt:
            channel_focus = (extra_txt, None)
        else:
            kind = "hot_take"
    if kind == "channel":
        up = con.execute("SELECT video_id, title, url, summary FROM uploads WHERE used=0 "
                         "ORDER BY published DESC LIMIT 1").fetchone()
        if up:
            topic = f"yt:{up[0]}"
            channel_focus = (f"FOCUS ON THIS NEW UPLOAD (hype it): {up[1]}\nMoments from it:\n{up[3] or ''}", up[2])
        else:
            channel_focus = ("FOCUS: pick one funny moment, quote or lore bit from YOUR CHANNEL above.", None)
    print(f"Post type this time: {kind}" + (f" | topic: {topic}" if topic else ""), flush=True)
    best = con.execute("SELECT text FROM posts WHERE score IS NOT NULL AND score > 0 AND length(text) < 220 "
                       "ORDER BY score DESC LIMIT 3").fetchall()
    recent = [t for (t,) in con.execute("SELECT text FROM posts ORDER BY id DESC LIMIT 6").fetchall()]
    worn = worn_phrases(con)
    extra = ""
    if best:
        extra += "Posts of yours that did well:\n" + "\n".join(f"- {t}" for (t,) in best) + "\n"
    if recent:
        extra += ("What your last posts were about (pick a DIFFERENT subject and a different opening):\n"
                  + "\n".join(f"- {t[:90]}" for t in recent) + "\n")
    if worn:
        extra += ("PHRASES YOU'VE WORN OUT (do NOT use any of these): " + "; ".join(f'"{w}"' for w in worn) + "\n")
    if channel_focus:
        extra += "\n" + channel_focus[0] + "\n"
    if feedback and kind == "stats_update":
        extra += "\nYOUR REAL NUMBERS RIGHT NOW (only use numbers from here):\n" + feedback + "\n"
    extra += recall(f"{POST_KINDS[kind]} {topic or ''} {channel_focus[0] if channel_focus else ''}", "threads")
    base = persona_for(kind, [topic] if topic and kind in NEWS_KINDS else None)
    fresh, rejected = [], []
    for attempt in range(2):
        nudge = ("" if not attempt else "\nYour last drafts were rejected for: " + "; ".join(sorted(set(rejected))[:4]) +
                 ". Go SIMPLER: one plain, funny sentence each.")
        raw = ask(f"{base}\n\n{extra}{nudge}\n"
                  "Write 6 different candidate posts, each about a different angle. Reply with only a JSON array of 6 "
                  "strings.", temperature=0.8)
        parsed = json_list(raw)
        if not parsed:
            print("Model didn't return a usable list. Raw output:\n", (raw or "")[:600])
            continue
        for c in parsed:
            c = _mm_words.clean(tidy(c), "threads", "post") if isinstance(c, str) else ""
            if not c or "http" in c:
                continue
            if too_morbid(c, kind):
                print(f"[dropped: morbid] {c[:90]}")
                continue
            if too_similar(con, c):
                print(f"[dropped: too much like a recent post] {c[:90]}")
                continue
            why = draft_problem(c, kind, worn)
            if why:
                rejected.append(why)
                print(f"[dropped: {why}] {c[:90]}")
                continue
            fresh.append(c)
        if len(fresh) >= 2:
            break
    return fresh, kind, topic


def worn_phrases(con):
    """Phrases the bot has to stop using: its recent verbal tics + known crutches it used in the last 12 posts."""
    recent = " ".join(t.lower() for (t,) in con.execute("SELECT text FROM posts ORDER BY id DESC LIMIT 12") if t)
    return overused_phrases(con) + [t for t in TICS if t in recent]


def _wset(t):
    return set(re.findall(r"[a-z0-9']{3,}", (t or "").lower()))


def too_similar(con, text, n=40, limit=0.5):
    """True if this draft is basically a repeat of one of the last n posts."""
    w = _wset(text)
    if len(w) < 4:
        return False
    for (old,) in con.execute("SELECT text FROM posts ORDER BY id DESC LIMIT ?", (n,)):
        o = _wset(old)
        if o and len(w & o) / min(len(w), len(o)) >= limit:
            return True
    return False


def overused_phrases(con, n=15, k=8):
    """3-word phrases that showed up in 3+ of the last n posts (the bot's verbal tics)."""
    from collections import Counter
    cnt = Counter()
    for (t,) in con.execute("SELECT text FROM posts ORDER BY id DESC LIMIT ?", (n,)):
        ws = re.findall(r"[a-z0-9']+", (t or "").lower())
        cnt.update({" ".join(ws[i:i + 3]) for i in range(len(ws) - 2)})
    stop = {"i don't know", "if you are", "this is the", "in the game", "is the only", "one of the", "a lot of"}
    return [p for p, c in cnt.most_common(40) if c >= 3 and p not in stop][:k]


def cmd_draft():
    con = db()
    candidates, kind, topic = generate_candidates(con)
    if SAFETY_CHECK:
        print(f"Safety-checking {len(candidates)} drafts...", flush=True)
    safe = []
    for c in candidates:
        if not SAFETY_CHECK or safety_check(c):
            safe.append(c)
        else:
            print(f"[filtered by safety check] {c}")
    if not safe:
        print("No usable candidates. Run again.")
        return

    print("\nCandidates:")
    for i, c in enumerate(safe, 1):
        print(f"{i}. {c}")
    choice = input("\nPost which number? (Enter to skip) ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(safe)):
        print("Skipped.")
        return
    save_post(con, safe[int(choice) - 1], kind, topic)


def cmd_score():
    con = db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = con.execute(
        "SELECT id, media_id, flagged, text, kind FROM posts WHERE media_id IS NOT NULL "
        "AND deleted=0 AND scored_at IS NULL AND created_at < ?", (cutoff,)
    ).fetchall()
    if not rows:
        print("Nothing old enough to score yet.")
        return
    results = []
    for row_id, media_id, flagged, text, kind in rows:
        m = insights(media_id)
        reply_texts = replies(media_id)
        negative = sum(is_negative(t) for t in reply_texts)
        score = compute_score(m, negative, flagged)
        results.append((text, kind or "unknown", score, reply_texts))
        con.execute(
            "UPDATE posts SET likes=?, replies=?, reposts=?, quotes=?, views=?, "
            "negative_replies=?, score=?, scored_at=? WHERE id=?",
            (m.get("likes", 0), m.get("replies", 0), m.get("reposts", 0), m.get("quotes", 0),
             m.get("views", 0), negative, score, datetime.now(timezone.utc).isoformat(), row_id),
        )
        print(f"post {row_id}: score {score:.1f}  {m}  negative replies: {negative}")
    con.commit()
    reflect(con, results)  # learn from the results and rewrite the memory file


class reply_lock:
    """Only one copy of a reply job at a time (the 5-minute reply task and the 15-minute cycle can overlap)."""
    def __init__(self, name):
        self.path = f"{name}.lock"
        self.ok = False

    def __enter__(self):
        if os.path.exists(self.path) and time.time() - os.path.getmtime(self.path) < 15 * 60:
            return False
        open(self.path, "w").close()
        self.ok = True
        return True

    def __exit__(self, *a):
        if self.ok:
            try:
                os.remove(self.path)
            except OSError:
                pass


def conversation(media_id):
    """Every reply under a post, including replies to replies (people answering the bot)."""
    try:
        data = api("GET", f"{media_id}/conversation", fields="id,text,username")
        return [r for r in data.get("data", []) if r.get("text")]
    except SystemExit:
        return replies(media_id, full=True)


def cmd_thank(auto=False):
    """Reply to EVERYONE who replies: on its own posts (including replies to its replies), and on the replies it left
    on other people's posts. Asks you before each reply unless --auto."""
    with reply_lock("thank") as free:
        if not free:
            return print("Threads replies: another reply job is running; skipping")
        _thank(auto)


def _thank(auto=False):
    con = db()
    posts = con.execute(
        "SELECT media_id, text FROM posts WHERE media_id IS NOT NULL AND deleted=0 "
        "ORDER BY id DESC LIMIT 25"
    ).fetchall()
    try:  # its replies on other people's posts (outreach) and to mentions: people answer those too
        posts += con.execute("SELECT reply_id, our_reply FROM outreach WHERE reply_id IS NOT NULL AND at > ? "
                             "ORDER BY id DESC LIMIT 40",
                             ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),)).fetchall()
    except sqlite3.OperationalError:
        pass
    try:
        me = api("GET", "me", fields="username").get("username", profile.get("threads_username") or "").lower()
    except SystemExit:
        me = (profile.get("threads_username") or "").lower()
    mine = {r[0] for r in con.execute("SELECT our_reply FROM answered WHERE our_reply IS NOT NULL")}
    done = 0
    for media_id, post_text in posts:
        for r in conversation(media_id):
            if (r.get("username") or "").lower() == me or r.get("text") in mine:
                continue  # that's the bot itself
            if done >= MAX_REPLIES_PER_RUN:
                print(f"Hit the limit of {MAX_REPLIES_PER_RUN} replies this run.")
                return
            if con.execute("SELECT 1 FROM answered WHERE reply_id=?", (r["id"],)).fetchone():
                continue
            user = r.get("username", "someone")
            draft, why = write_reply("Threads", post_text, user, r["text"])
            if not draft:
                print(f"[Threads: no reply to @{user} yet ({why}); trying again next run]")
                continue
            print(f"\n@{user}: {r['text']}\n  -> {draft}")
            if not auto:
                choice = input("Send? (y = yes, s = skip forever, Enter = skip for now) ").strip().lower()
                if choice == "s":
                    con.execute("INSERT INTO answered VALUES (?,?,?,?,?)",
                                (r["id"], user, r["text"], None, datetime.now(timezone.utc).isoformat()))
                    con.commit()
                    continue
                if choice != "y":
                    continue
            if DRY_RUN:
                print("[DRY RUN] reply not sent")
            else:
                publish(draft, reply_to_id=r["id"])
                remember_person("threads", user, r["text"], draft)
                print("Sent!")
                con.execute("INSERT INTO answered VALUES (?,?,?,?,?)",
                            (r["id"], user, r["text"], draft, datetime.now(timezone.utc).isoformat()))
                con.commit()
            done += 1
    print(f"Done. {done} repl{'y' if done == 1 else 'ies'} handled.")


def cmd_flag(post_id):
    con = db()
    con.execute("UPDATE posts SET flagged=1, score=COALESCE(score,0)-50 WHERE id=?", (post_id,))
    con.commit()
    print(f"Post {post_id} flagged (-50).")


def cmd_delete(post_id):
    con = db()
    row = con.execute("SELECT media_id, text FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row or not row[0]:
        print("No posted Threads post with that id.")
        return
    if input(f"Delete from Threads: {row[1]!r}? (y/N) ").strip().lower() != "y":
        print("Cancelled.")
        return
    api("DELETE", row[0])
    con.execute("UPDATE posts SET deleted=1 WHERE id=?", (post_id,))
    con.commit()
    print("Deleted. It stays on the scoreboard so the bot still learns from it.")


def cmd_refresh():
    r = requests.get("https://graph.threads.net/refresh_access_token",
                     params={"grant_type": "th_refresh_token", "access_token": token()}, timeout=60)
    if r.status_code >= 400:
        sys.exit(f"Refresh failed {r.status_code}: {r.text}")
    print("New token received. Put it in secrets.env as THREADS_TOKEN=<the token below>")
    print(r.json()["access_token"])


def cmd_check():
    print(f"Model: {MODEL}")
    try:
        tags = requests.get("http://localhost:11434/api/tags", timeout=10).json()
        names = [m["name"] for m in tags.get("models", [])]
        print("Ollama: running. Installed models:", ", ".join(names) or "(none)")
        if not any(n == MODEL or n == MODEL + ":latest" for n in names):
            print(f"  !! {MODEL} is NOT installed. Run: ollama pull {MODEL}")
    except requests.exceptions.ConnectionError:
        print("Ollama: NOT running. Open the Ollama app.")
    me = api("GET", "me", fields="username")
    print(f"Threads token: OK, logged in as @{me.get('username')}")
    print(f"Dry run: {'ON (nothing gets posted)' if DRY_RUN else 'OFF (LIVE)'}")


def status_facts(con):
    """Plain facts for the chat: is the autopilot running, when each platform last posted, and why it's waiting."""
    out = []
    log = ""
    try:
        with open("bot.log", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 60000))
            log = f.read()
    except OSError:
        pass
    ticks = re.findall(r"######## machine tick (\d{4}-\d\d-\d\d \d\d:\d\d)", log)
    out.append(f"- Autopilot: last run started {ticks[-1]} (runs every 15 min)" if ticks else "- Autopilot: no runs in the log")
    now_ = datetime.now(timezone.utc)

    def ago(iso):
        if not iso:
            return "never"
        h = (now_ - datetime.fromisoformat(iso)).total_seconds() / 3600
        return f"{h:.1f}h ago" if h < 48 else f"{h / 24:.0f} days ago"
    last = con.execute("SELECT MAX(created_at) FROM posts WHERE media_id IS NOT NULL").fetchone()[0]
    n = con.execute("SELECT COUNT(*) FROM posts WHERE media_id IS NOT NULL AND created_at > ?",
                    ((now_ - timedelta(hours=24)).isoformat(),)).fetchone()[0]
    out.append(f"- Threads: last post {ago(last)}, {n} in the last 24h")
    for plat in ("youtube", "tiktok", "instagram", "facebook"):
        try:
            last, n = con.execute("SELECT MAX(posted_at), SUM(posted_at > ?) FROM mm_posts WHERE platform=?",
                                  ((now_ - timedelta(hours=24)).isoformat(), plat)).fetchone()
            out.append(f"- {plat}: last post {ago(last)}, {n or 0} in the last 24h")
        except Exception:
            pass
    block = log[log.rfind("######## machine tick"):] if ticks else ""
    why = [l.strip() for l in block.splitlines() if re.search(r"not posting|skipped|failed|waiting|Replies done|posted|uploaded", l)]
    if why:
        out.append("- Latest run said:\n" + "\n".join(f"    {w[:200]}" for w in why[:12]))
    return "\n".join(out)


def _chat_job(kind, payload):
    """Run a chat-requested job (posting, AI video, clips...) in its OWN process. When it finishes, every bit of
    RAM it used (Whisper, video frames, AI) goes back to Windows; the chat itself stays small."""
    import subprocess
    try:
        p = subprocess.Popen([sys.executable, "threadsbot.py", "chat-job", kind], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
        for line in p.stdout:
            print(line, end="", flush=True)
        if p.wait() != 0:
            print("Bot: That job stopped with an error (see above).", flush=True)
    except Exception as e:
        print(f"Bot: That failed: {str(e)[:200]}", flush=True)


def _chat_job_main(kind):
    import mm_chatcmd
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        if kind == "post":
            for line in mm_chatcmd.run(payload["act"], payload.get("text")):
                print(f"Bot: {line}", flush=True)
        else:
            mm_chatcmd.other_action(kind, payload.get("d") or {})
    except (SystemExit, Exception) as e:
        print(f"Bot: That failed: {str(e)[:200]}", flush=True)
        sys.exit(1)


def cmd_chat():
    """Private chat with the bot. Here it is a normal, direct AI assistant (not the the creator persona): it answers
    questions about what the machine is doing using real status facts. 'remember ...' saves a note."""
    con = db()
    history, last_reply = [], ""
    pending, pending_text = None, None  # a post the owner asked for, waiting for their "yes"
    print("\nChat with your bot. Talk normally: ask questions, tell it to post or make anything, change settings, "
          "or tell it to remember something. 'quit' to leave.\n")
    while True:
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not msg:
            continue
        if msg.lower() in ("quit", "exit", "q"):
            break
        import mm_chatcmd
        facts = status_facts(con)
        pdesc = None
        if pending:
            pdesc = mm_chatcmd.describe(pending) + (f" | Threads text: {pending_text}" if pending_text else "")
        d = mm_chatcmd.route(ask, msg, facts, _memory_only(), history, pdesc)
        intent = d.get("intent", "chat")
        reply = (d.get("reply") or "").strip()

        def do_post(act, text):
            print("Bot: Posting now (pictures/videos can take a few minutes)...", flush=True)
            _chat_job("post", {"act": act, "text": text})
            history.append(f"[OWNER]: {msg}\nAssistant: Posted: {mm_chatcmd.describe(act)}")

        if intent == "confirm" and pending:
            do_post(pending, pending_text)
            pending, pending_text = None, None
        elif intent == "cancel" and pending:
            pending, pending_text = None, None
            print("Bot: Cancelled, nothing was posted.")
            history.append(f"[OWNER]: {msg}\nAssistant: Cancelled.")
        elif intent == "revise" and pending:
            if "threads" in pending["platforms"] and pending_text:
                pending_text = _mm_words.clean(tidy(ask(f"{IDENTITY}\n\nRewrite this Threads post the way [OWNER] asks. "
                                                        f"Only the post text.\nPost: {pending_text}\n[OWNER]: {msg}",
                                                        temperature=0.7)), "threads", "post")
            else:
                pending["about"] = f"{pending.get('about') or ''} ({d.get('about') or msg})".strip()
            print(f"Bot: New version: {mm_chatcmd.describe(pending)}"
                  + (f"\n  Threads text: {pending_text}" if pending_text else "") + "\nGood to post?")
        elif intent == "post":
            act = d
            text = mm_chatcmd.draft_threads(ask, act) if "threads" in act["platforms"] else None
            if d.get("sure"):
                if text:
                    print(f"Bot: Threads text: {text}")
                do_post(act, text)
            else:
                pending, pending_text = act, text
                print(f"Bot: {reply + chr(10) if reply else ''}Plan: {mm_chatcmd.describe(act)}"
                      + (f"\n  Threads text: {text}" if text else "") + "\nWant me to post it?")
        elif intent in ("full_ai", "make", "replies", "cycle"):
            print(f"Bot: {reply or 'On it.'}", flush=True)
            _chat_job(intent, {"d": d})
            history.append(f"[OWNER]: {msg}\nAssistant: did {intent}")
        elif intent == "setting":
            changed = mm_chatcmd.apply_settings(d.get("settings"))
            print("Bot: " + (("Changed: " + "; ".join(changed)) if changed else (reply or "Nothing to change.")))
            history.append(f"[OWNER]: {msg}\nAssistant: settings {changed}")
        elif intent == "remember":
            note = (d.get("note") or msg).strip()
            with open(MEMORY_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n- Note from the owner: {note}")
            print(f"Bot: Saved to memory: {note}")
            history.append(f"[OWNER]: {msg}\nAssistant: Saved to memory.")
        else:
            if not reply or (last_reply and len(_wset(reply) & _wset(last_reply)) /
                             max(1, min(len(_wset(reply)), len(_wset(last_reply)))) >= 0.6):
                reply = ask(f"Answer [OWNER] directly and briefly as a plain assistant, with something new.\nLIVE STATUS:\n"
                            f"{facts}\n[OWNER]: {msg}\nAssistant:", temperature=0.6).strip()
            last_reply = reply
            history.append(f"[OWNER]: {msg}\nAssistant: {reply}")
            print(f"Bot: {reply}")
        print()
    said = [h.split("\nAssistant:")[0][6:] for h in history]
    if said:
        raw = ask("From these messages [OWNER] sent their bot, list ONLY lasting instructions, preferences, facts or content "
                  "ideas the bot should remember (one short line each). Questions about status are NOT worth remembering. "
                  "If there's nothing, reply NONE.\n\n" + "\n".join(said), temperature=0.2).strip()
        keep = [l.strip("-• ").strip() for l in raw.splitlines() if l.strip() and "NONE" not in l.upper()][:6]
        if keep:
            with open(MEMORY_FILE, "a", encoding="utf-8") as f:
                for k in keep:
                    f.write(f"\n- Note from the owner: {k}")
            print("Saved to memory from this chat:\n" + "\n".join(f"  - {k}" for k in keep))


def cmd_report():
    rows = db().execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    if not rows:
        print("No posts yet. Use Draft first.")
        return
    con = db()
    for row in con.execute(
        "SELECT id, score, likes, replies, reposts, quotes, views, negative_replies, deleted, text "
        "FROM posts ORDER BY score DESC NULLS LAST"
    ):
        print(row)


if __name__ == "__main__":
    simple = {"draft": cmd_draft, "score": cmd_score, "report": cmd_report,
              "refresh": cmd_refresh, "thank": cmd_thank, "check": cmd_check, "auto": cmd_auto,
              "channel": lambda: refresh_channel(db(), force=True)}
    if len(sys.argv) == 2 and sys.argv[1] in simple:
        simple[sys.argv[1]]()
    elif len(sys.argv) == 3 and sys.argv[1] == "thank" and sys.argv[2] == "--auto":
        cmd_thank(auto=True)
    elif len(sys.argv) == 3 and sys.argv[1] == "flag":
        cmd_flag(int(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == "chat":
        cmd_chat()
    elif len(sys.argv) == 3 and sys.argv[1] == "chat-job":
        _chat_job_main(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "delete":
        cmd_delete(int(sys.argv[2]))
    else:
        print(__doc__)
