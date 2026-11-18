"""
Content flow: recurring SERIES, WINNER REMIXES, FAN-COMMENT content, and a SELF-CRITIC that grades drafts before posting.

- Series: recurring numbered formats ("Weekly Rage Report #12") that regulars come back for. The brain learns which
  series do best. the owner can add or edit them in the app (Settings -> Series), and the bot can start new ones from
  winners.
- Winner remix: when a post clearly beats the creator's usual (advantage > 0.5, about 65% better), ideas get queued:
  a Threads sequel, an Instagram meme of the same joke, another clip from the same video, or an AI-video sequel.
- Fan comments: once a day, the best comments from all platforms can become content ("you asked for it, @x").
- Self-critic: several drafts, one grading call (hook, funny, in-character, makes people comment), and the
  winner gets one punch-up. Same model, one extra call.
"""
import json
import random
import re
from datetime import datetime, timedelta, timezone

import creator_profile as profile

WIN = 0.5  # advantage that counts as a clear winner (log scale: ~65% better than usual)
SEED_SERIES = [tuple(x) for x in (profile.get("series") or []) if len(x) == 2]  # profile.json "series"


def now():
    return datetime.now(timezone.utc)


def ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, about TEXT, "
                "episode INTEGER DEFAULT 0, last_at TEXT, last_text TEXT, active INTEGER DEFAULT 1, made_by TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS ideas (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, target TEXT, "
                "payload TEXT, status TEXT DEFAULT 'new', used_at TEXT)")
    if not con.execute("SELECT 1 FROM series").fetchone():
        con.executemany("INSERT OR IGNORE INTO series (name, about, made_by) VALUES (?,?, 'starter')", SEED_SERIES)
    con.commit()


# ======================= series =======================
def active_series(con):
    ensure(con)
    return con.execute("SELECT id, name, about, episode, last_text FROM series WHERE active=1").fetchall()


def series_prompt(con):
    """(extra prompt text, topic) for a Threads series episode, or (None, None) if there are no series."""
    rows = active_series(con)
    if not rows:
        return None, None
    import mm_brain
    name = mm_brain.choose(con, "series", [r[1] for r in rows])
    sid, name, about, ep, last = next(r for r in rows if r[1] == name)
    txt = (f"\nRECURRING SERIES: this is \"{name} #{ep + 1}\". What the series is: {about}\nStart the post with "
           f"\"{name} #{ep + 1}:\". Make it feel like a show people come back for.")
    if last:
        txt += f" Last episode was: \"{last[:200]}\" (don't repeat it; a callback is fine)."
    return txt, f"series:{sid}"


def posted_series(con, topic, text):
    if topic and topic.startswith("series:"):
        con.execute("UPDATE series SET episode=episode+1, last_at=?, last_text=? WHERE id=?",
                    (now().isoformat(), text[:300], int(topic.split(":")[1])))
        con.commit()


def series_name(con, topic):
    if topic and topic.startswith("series:"):
        r = con.execute("SELECT name FROM series WHERE id=?", (int(topic.split(":")[1]),)).fetchone()
        return r[0] if r else None
    return None


# ======================= ideas (remixes + fan content) =======================
def add_idea(con, kind, target, payload):
    ensure(con)
    key = json.dumps(payload, sort_keys=True)
    if con.execute("SELECT 1 FROM ideas WHERE kind=? AND target=? AND payload=?", (kind, target, key)).fetchone():
        return
    con.execute("INSERT INTO ideas (at, kind, target, payload) VALUES (?,?,?,?)", (now().isoformat(), kind, target, key))
    con.commit()
    print(f"[ideas] queued a {kind} for {target}: {str(payload)[:120]}")


def next_idea(con, target, max_age_days=5):
    ensure(con)
    r = con.execute("SELECT id, kind, payload FROM ideas WHERE target=? AND status='new' AND at > ? ORDER BY id LIMIT 1",
                    (target, (now() - timedelta(days=max_age_days)).isoformat())).fetchone()
    return (r[0], r[1], json.loads(r[2])) if r else None


def use_idea(con, idea_id):
    if idea_id:
        con.execute("UPDATE ideas SET status='used', used_at=? WHERE id=?", (now().isoformat(), idea_id))
        con.commit()


def queue_remixes(con, settled):
    """settled: list of (platform, kind, text, score, advantage, info) from mm_brain.settle."""
    for plat, kind, text, score, adv, info in settled:
        if adv < WIN:
            continue
        pct = f"beat your usual by a LOT on {plat}"
        if plat == "threads":
            add_idea(con, "remix", "threads", {"text": text[:300], "why": pct, "series": info.get("series")})
            add_idea(con, "remix", "instagram", {"text": text[:300], "why": pct})
        elif info.get("clip_id"):
            add_idea(con, "remix", "video", {"clip_id": info["clip_id"], "title": text[:150], "why": pct})
            add_idea(con, "remix", "threads", {"text": f"your clip \"{text[:150]}\"", "why": pct, "hype": True})
        else:  # Instagram picture post
            add_idea(con, "remix", "instagram", {"text": text[:300], "why": pct, "kind": kind})


def find_fan_content(con, ask):
    """Once a day: pick the best fan comments of the last 3 days to turn into content."""
    ensure(con)
    r = con.execute("SELECT value FROM meta WHERE key='fan_content_at'").fetchone()
    if r and now() - datetime.fromisoformat(r[0]) < timedelta(hours=24):
        return
    con.execute("INSERT OR REPLACE INTO meta VALUES ('fan_content_at', ?)", (now().isoformat(),))
    con.commit()
    since = (now() - timedelta(days=3)).isoformat()
    rows = []
    for table, plat, cols in (("answered", "threads", "username, their_text, answered_at"),
                              ("ig_answered", "instagram", "username, text, at"),
                              ("yt_answered", "youtube", "username, text, at")):
        try:
            rows += [(plat, u, t) for u, t, a in con.execute(f"SELECT {cols} FROM {table} WHERE {cols.split(', ')[2]} > ?",
                                                             (since,)).fetchall()
                     if t and len(t.split()) >= 4 and "http" not in t]
        except Exception:
            pass
    seen, uniq = set(), []
    for p, u, t in rows:  # one comment per person, so a spammer can't fill the list
        if (u or "").lower() not in seen:
            seen.add((u or "").lower())
            uniq.append((p, u, t))
    if not uniq:
        return
    uniq = uniq[-25:]
    listing = "\n".join(f"{i}. ({p}) @{u}: {t[:200]}" for i, (p, u, t) in enumerate(uniq))
    raw = ask("You run content for [CREATOR], a [NICHE] creator. Fans left these comments:\n" + listing +
              "\n\nPick up to 2 that would make a FUNNY post if [CREATOR] turned them into content (a joke they made, a "
              "dare, a question, a dumb idea, a roast of [CREATOR]). Skip anything mean about other people, personal, "
              "sexual, or asking [CREATOR] to post their image or link. Reply ONLY JSON list: [{\"index\": 0, \"format\": "
              "\"threads\" or \"meme\", \"idea\": \"what to post, one line\"}] or [] if none are good.", temperature=0.4)
    try:
        picks = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
    except Exception:
        return
    for pk in picks[:2]:
        try:
            p, u, t = uniq[int(pk["index"])]
        except Exception:
            continue
        add_idea(con, "fan", "threads" if pk.get("format") != "meme" else "instagram",
                 {"user": u, "platform": p, "comment": t[:300], "idea": pk.get("idea", "")[:200]})


def idea_prompt(kind, p):
    """Prompt text for using an idea in a post."""
    if kind == "fan":
        return (f"\nFAN-INSPIRED POST: @{p['user']} commented on your {p['platform']}: \"{p['comment']}\". "
                f"Idea: {p.get('idea', '')}. Turn it into a post and give them credit (\"you asked for it, @{p['user']}\" "
                "energy). Make them feel famous. Never make fun of them in a mean way.")
    if p.get("hype"):
        return (f"\nYOUR CLIP IS POPPING OFF: {p['text']} {p['why']}. Brag about it like a maniac, thank everyone, "
                "and tell people to go see it.")
    return (f"\nREMIX A WINNER: this post {p['why']}: \"{p['text']}\". Make a SEQUEL / part 2 that builds on the same "
            "joke with a new twist. Don't copy it.")


def threads_idea(con):
    """(kind, extra prompt, topic) if there's a queued idea for Threads."""
    i = next_idea(con, "threads")
    if not i:
        return None
    iid, kind, p = i
    return ("fan_shoutout" if kind == "fan" else "remix"), idea_prompt(kind, p), f"idea:{iid}"


def posted_idea(con, topic):
    if topic and topic.startswith("idea:"):
        use_idea(con, int(topic.split(":")[1]))


def maybe_new_series(con, ask, text):
    """A big Threads winner might deserve to become a recurring series (max 6 active)."""
    if len(active_series(con)) >= 6:
        return
    raw = ask(f"This [CREATOR] post did amazing: \"{text[:300]}\"\nCould it become a RECURRING series (a format to repeat "
              "with new episodes)? Reply ONLY JSON: {\"series\": true or false, \"name\": \"short series name\", "
              "\"about\": \"what each episode is, one line\"}", temperature=0.3)
    try:
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        return
    if d.get("series") is True and d.get("name") and d.get("about"):
        con.execute("INSERT OR IGNORE INTO series (name, about, made_by) VALUES (?,?, 'bot')",
                    (d["name"][:40], d["about"][:300]))
        con.commit()
        print(f"[series] the bot started a new series: {d['name']}")


def tick(con, ask, settled):
    ensure(con)
    queue_remixes(con, settled)
    for plat, kind, text, score, adv, info in settled:
        if plat == "threads" and adv >= 0.8 and not info.get("series"):
            maybe_new_series(con, ask, text)
    find_fan_content(con, ask)


# ======================= self-critic =======================
def critique(ask, identity, platform, drafts, context=""):
    """Grades drafts (list of strings) in ONE call. Returns (best_index, improved_text_or_None, scores)."""
    if not drafts:
        return 0, None, []
    if len(drafts) == 1 and not context:
        return 0, None, []
    listing = "\n".join(f"{i}. {d}" for i, d in enumerate(drafts))
    raw = ask(f"{identity[:2500]}\n\n{context}\n\nYou're your own harsh editor. These are drafts for {platform}:\n{listing}\n\n"
              "Score each 1-10 overall. What makes a draft GOOD: CLEAR (a stranger who never saw your channel gets it "
              "instantly; one idea, no word soup), HOOK (the first words stop the scroll), FUNNY (the joke actually "
              "lands), YOUR VOICE (deadpan and dumb, not a brand), ANSWERABLE (people can reply to it or argue). "
              "Mark DOWN hard: rambling, several unrelated things mashed together, inside jokes nobody would get, "
              "begging for likes/comments, talking about views or the algorithm. Shorter usually wins.\n"
              "Then take the best one and punch it up: same idea, tighter and funnier, NEVER longer, nothing new "
              "added. Reply ONLY JSON: {\"scores\": [score for each draft], \"best\": index, "
              "\"improved\": \"the punched-up best draft\"}", temperature=0.3)
    try:
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        best = int(d.get("best", 0))
        if not 0 <= best < len(drafts):
            best = 0
        imp = d.get("improved")
        return best, (imp.strip() if isinstance(imp, str) and imp.strip() else None), d.get("scores", [])
    except Exception:
        return 0, None, []
