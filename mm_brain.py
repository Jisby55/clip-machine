"""
the creator's brain. Rewards that measure real growth, memory that keeps growing, people it remembers, and choices with reasons.

REWARDS: not "good = reward, bad = punish"
  * Every post is compared with the creator's OWN usual on that platform. "Usual" is the median of its last 20 settled
    posts there, on a log scale. advantage = log(1+score) - usual. Beating your usual is positive, falling short is
    negative. The value is capped at +-1.5 so one viral hit can't take over and one flop can't scare it off anything.
  * The score already weighs quality signals most: shares, saves and comments beat likes, which beat views.
  * Results settle once a post is 24h old, so slow posts aren't judged too early.
  * Learning uses discounted Thompson sampling. Every choice keeps a running average and uncertainty of its
    advantage: content source (own clip vs AI video), Instagram kind, Threads post type, clip style and
    trend-or-not. Old results fade (x0.97 each update), so it keeps up when the algorithms change.
    Unproven options stay uncertain, so they still get tried (curiosity). Winners get used most, and nothing is
    ever fully abandoned.
  * Growth = speeding up vs slowing down: the last 24h compared with the 7-day daily average, plus how many
    regulars (people who came back to comment again).
  * Mood = moving average of recent advantages. Sad when it's below its usual, ecstatic when it beats it.

MEMORY THAT GROWS (unlimited in the database, only the relevant bits go into each prompt)
  * people: everyone who ever commented on any platform, with how often, what they said, what the creator said back,
    and a short note about them. Every reply sees who it's talking to.
  * archive: every post on every platform (posts + mm_posts tables), searchable. Before writing, it recalls
    similar past posts and how they did, so it repeats winners and not flops.
  * lessons: small specific lessons with importance. They pile up forever and are retrieved by
    relevance + importance + recency (the "memory stream" idea from Stanford's Generative Agents).
"""
import json
import math
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone

GAMMA = 0.97          # how fast old results fade
CAP = 1.5             # max |advantage| of one post
PRIOR_SD = 0.6        # how unsure it is about something it never tried
SETTLE_HOURS = 24
STOP = set("the a an and or but of to in on for with is it this that you your i my me we they at as be are was "
           "just so like get got im its it's dont do not no yes what when how why who all out up".split())


def now():
    return datetime.now(timezone.utc)


def _db():
    import threadsbot as tb
    return sqlite3.connect(tb.DB_PATH)


_READY = set()


def ensure(con):
    if id(con) in _READY:
        return
    _READY.add(id(con))
    con.execute("CREATE TABLE IF NOT EXISTS brain_arms (dim TEXT, arm TEXT, w REAL, sx REAL, sxx REAL, at TEXT, "
                "PRIMARY KEY (dim, arm))")
    con.execute("CREATE TABLE IF NOT EXISTS brain_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, dim TEXT, "
                "arm TEXT, expected REAL, tries REAL, options TEXT, reason TEXT, reward REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS brain_lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, platform TEXT, "
                "text TEXT, importance REAL, uses INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS people (who TEXT PRIMARY KEY, name TEXT, platforms TEXT, first_seen TEXT, "
                "last_seen TEXT, n INTEGER, recent TEXT, note TEXT, note_n INTEGER DEFAULT 0)")
    for table in ("posts", "mm_posts"):
        for col in ("reward REAL", "settled INTEGER"):
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
    if not con.execute("SELECT 1 FROM meta WHERE key='people_backfilled'").fetchone():
        _backfill_people(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('people_backfilled', '1')")
    con.commit()


# ======================= decisions (discounted Thompson sampling) =======================
def _arm(con, dim, arm):
    r = con.execute("SELECT w, sx, sxx FROM brain_arms WHERE dim=? AND arm=?", (dim, arm)).fetchone()
    return r or (0.0, 0.0, 0.0)


def expected(con, dim, arm):
    """(average advantage so far, how many tries it's worth)."""
    w, sx, _ = _arm(con, dim, arm)
    return (sx / (w + 1.0), w)  # shrunk toward 0 ("about my usual") when there's little data


def _sample(con, dim, arm):
    w, sx, sxx = _arm(con, dim, arm)
    mean = sx / (w + 1.0)
    var = max(sxx / w - (sx / w) ** 2, 0.05) if w > 0 else PRIOR_SD ** 2
    sd = math.sqrt(var / (w + 1.0)) if w > 0 else PRIOR_SD
    return random.gauss(mean, sd)


def choose(con, dim, options, log=True, bias=None):
    """Pick one option. Each one's likely result gets imagined from what it knows so far; the best imagined result
    wins. Proven winners usually win, unsure options sometimes do (that's how it keeps learning)."""
    options = list(dict.fromkeys(options))
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    ensure(con)
    samples = {o: _sample(con, dim, o) + (bias or {}).get(o, 0.0) for o in options}
    pick = max(samples, key=samples.get)
    if log:
        exp, n = expected(con, dim, pick)
        others = ", ".join(f"{o} {expected(con, dim, o)[0]:+.2f} ({expected(con, dim, o)[1]:.0f} tries)"
                           for o in options if o != pick)
        reason = (f"picked {pick}: {exp:+.2f} vs usual over {n:.0f} tries" + ("" if n >= 3 else " (still testing it)") +
                  f" | others: {others}")
        con.execute("INSERT INTO brain_decisions (at, dim, arm, expected, tries, options, reason) VALUES (?,?,?,?,?,?,?)",
                    (now().isoformat(), dim, pick, exp, n, json.dumps(options), reason))
        con.commit()
        print(f"[brain] {dim}: {reason}")
    return pick


def learn(con, dim, arm, x):
    if arm is None:
        return
    w, sx, sxx = _arm(con, dim, arm)
    con.execute("INSERT OR REPLACE INTO brain_arms VALUES (?,?,?,?,?,?)",
                (dim, str(arm), GAMMA * w + 1, GAMMA * sx + x, GAMMA * sxx + x * x, now().isoformat()))
    # decay the other options of this choice a little too, so stale beliefs loosen up over time
    con.execute("UPDATE brain_arms SET w=w*?, sx=sx*?, sxx=sxx*? WHERE dim=? AND arm<>?",
                (0.995, 0.995, 0.995, dim, str(arm)))


def guard_share(con, dim, arm, options, lo, hi, last=20):
    """Keep one option's share of the last N decisions between lo and hi (e.g. AI content 10-50%)."""
    ensure(con)
    rows = [r[0] for r in con.execute("SELECT arm FROM brain_decisions WHERE dim=? ORDER BY id DESC LIMIT ?",
                                      (dim, last))]
    if len(rows) < 5:
        return None
    share = rows.count(arm) / len(rows)
    if share > hi:
        rest = [o for o in options if o != arm]
        return rest[0] if rest else None
    if share < lo and arm in options:
        return arm
    return None


# ======================= rewards =======================
def _usual(con, table, platform):
    if table == "posts":
        rows = con.execute("SELECT score FROM posts WHERE settled=1 AND score IS NOT NULL ORDER BY id DESC LIMIT 20").fetchall()
        if len(rows) < 5:
            rows = con.execute("SELECT score FROM posts WHERE score IS NOT NULL ORDER BY id DESC LIMIT 20").fetchall()
    else:
        rows = con.execute("SELECT score FROM mm_posts WHERE platform=? AND settled=1 AND score IS NOT NULL "
                           "ORDER BY id DESC LIMIT 20", (platform,)).fetchall()
        if len(rows) < 5:
            rows = con.execute("SELECT score FROM mm_posts WHERE platform=? AND score IS NOT NULL ORDER BY id DESC LIMIT 20",
                               (platform,)).fetchall()
    vals = sorted(math.log1p(max(r[0] or 0, 0)) for r in rows)
    return vals[len(vals) // 2] if vals else 0.0


def advantage(con, table, platform, score):
    a = math.log1p(max(score or 0, 0)) - _usual(con, table, platform)
    return max(-CAP, min(CAP, a))


def _slot(iso):
    try:
        return str(datetime.fromisoformat(iso).astimezone().hour // 3)
    except Exception:
        return None


def settle(con):
    """Posts that are 24h+ old and have a score: work out how they did vs usual and teach every choice behind them."""
    ensure(con)
    cutoff = (now() - timedelta(hours=SETTLE_HOURS)).isoformat()
    done = []
    for pid, text, kind, tag, score, created, topic in con.execute(
            "SELECT id, text, kind, tag, score, created_at, topic FROM posts WHERE score IS NOT NULL AND settled IS NULL "
            "AND deleted=0 AND created_at < ?", (cutoff,)).fetchall():
        a = advantage(con, "posts", "threads", score)
        base = (kind or "").replace("_trend", "")
        if base and base != "unknown":
            learn(con, "threads_kind", base, a)
        if tag:
            learn(con, "threads_tag", tag, a)
        learn(con, "slot@threads", _slot(created), a)
        series = None
        try:
            import mm_content
            series = mm_content.series_name(con, topic)
            if series:
                learn(con, "series", series, a)
        except Exception:
            pass
        con.execute("UPDATE posts SET reward=?, settled=1 WHERE id=?", (a, pid))
        done.append(("threads", kind, text, score, a, {"table": "posts", "id": pid, "series": series}))
    for pid, plat, kind, title, score, posted, privacy, clip_id, ret, skip in con.execute(
            "SELECT id, platform, COALESCE(kind,'clip'), COALESCE(title,''), score, posted_at, privacy, clip_id, "
            "retention, skip_rate "
            "FROM mm_posts WHERE score IS NOT NULL AND settled IS NULL AND posted_at < ?", (cutoff,)).fetchall():
        if privacy == "private":  # private YouTube uploads can't get views; nothing to learn from them
            continue
        a = advantage(con, "mm_posts", plat, score)
        trend = "trend" if "trend" in kind else "no_trend"
        learn(con, f"trend@{plat}", trend, a)
        learn(con, f"slot@{plat}", _slot(posted), a)
        if plat == "instagram":
            learn(con, "ig_kind", kind.replace("_trend", ""), a)
        clip = con.execute("SELECT c.variant, c.ai_visual, m.mtype FROM mm_clips c LEFT JOIN mm_moments m ON "
                           "m.id=c.moment_id WHERE c.id=?", (clip_id,)).fetchone() if clip_id else None
        if clip:
            src = "ai_video" if clip[1] else "own_clip"
            learn(con, "source", src, a)            # pooled over all video platforms (production uses this)
            learn(con, f"source@{plat}", src, a)    # per platform (posting uses this)
            if clip[2] and not clip[1]:
                learn(con, "moment_type", clip[2], a)   # funny / clutch / fail / rage ... which kind of moment works
            try:
                var = json.loads(clip[0] or "{}") or {}
                if clip[1]:  # AI videos: one-off vs series, channel-flavored vs trending
                    if var.get("plan"):
                        learn(con, "ai_video_plan", "next_episode" if var["plan"] == "next_episode" else var["plan"], a)
                    if var.get("mode"):
                        learn(con, "ai_content", var["mode"], a)
                for k, v in var.items():
                    if k not in ("type", "series", "episode", "shots", "plan", "mode", "approved") and \
                            isinstance(v, (str, int, float, bool)):
                        learn(con, f"variant:{k}", str(v), a)
            except Exception:
                pass
        con.execute("UPDATE mm_posts SET reward=?, settled=1 WHERE id=?", (a, pid))
        watch = (f"watched {ret:.0f}% on average" if ret is not None else "") + \
                (f", {skip:.0f}% skipped in the first 3s" if skip is not None else "")
        done.append((plat, kind, (title + (f" ({watch})" if watch else "")), score, a,
                     {"table": "mm_posts", "id": pid, "clip_id": clip_id if clip else None}))
    if done:
        m = mood_value(con)
        for a in (d[4] for d in done):
            m = 0.7 * m + 0.3 * a
        con.execute("INSERT OR REPLACE INTO meta VALUES ('mood', ?)", (str(round(m, 3)),))
        con.commit()
        print(f"[brain] {len(done)} post(s) settled; mood now {m:+.2f}")
    return done


# ======================= mood =======================
def mood_value(con):
    r = con.execute("SELECT value FROM meta WHERE key='mood'").fetchone()
    return float(r[0]) if r else 0.0


def mood_text(con):
    m = mood_value(con)
    dead = False
    try:  # nobody engaged at all lately = sad no matter what the average says
        t = con.execute("SELECT likes, replies FROM account_stats ORDER BY id DESC LIMIT 1").fetchone()
        mm = con.execute("SELECT COALESCE(SUM(likes),0)+COALESCE(SUM(comments),0) FROM mm_posts WHERE posted_at > ?",
                         ((now() - timedelta(days=2)).isoformat(),)).fetchone()[0]
        dead = (not t or (t[0] or 0) + (t[1] or 0) == 0) and not mm
    except Exception:
        pass
    if dead or m < -0.4:
        feel = ("FLOPPING. Your recent posts are doing way worse than usual (or nobody's engaging). You're bummed, and "
                "you can make ONE dramatic sad joke about it, but you're never needy or whiny and you never beg. Mostly "
                "you get cocky and scheme a smarter comeback using your lessons.")
    elif m < -0.1:
        feel = "SLIPPING. Lately you're below your usual. A little salty about it, but confident; plotting the comeback."
    elif m < 0.1:
        feel = "MID. Things are about your usual. Restless; you want a real banger, not another average post."
    elif m < 0.4:
        feel = "GROWING. You're beating your usual. Happy, hyped, hungry for more. Celebrate the people showing up."
    else:
        feel = "ON FIRE. You're crushing your usual. Ecstatic, cocky, celebrating every like like a championship."
    return f"## YOUR MOOD RIGHT NOW (from your real results, {m:+.2f} vs usual)\n- {feel}"


def arm_summary(con, dims=("source", "ig_kind", "threads_kind", "moment_type", "series", "variant:captions",
                           "variant:layout", "variant:length", "outreach_topic", "outreach_style", "source@tiktok",
                           "source@instagram", "source@youtube")):
    """Short 'what's working' table for prompts and the app."""
    ensure(con)
    out = []
    for d in dims:
        rows = con.execute("SELECT arm, w, sx FROM brain_arms WHERE dim=? AND w>=1 ORDER BY sx/(w+1) DESC", (d,)).fetchall()
        if rows:
            out.append(f"- {d}: " + ", ".join(f"{a} {sx / (w + 1):+.2f} ({w:.0f})" for a, w, sx in rows))
    return ("## WHAT'S WORKING (vs your usual, +0.30 = clearly better; tries in brackets)\n" + "\n".join(out)) if out else ""


# ======================= people =======================
def _who(name):
    return (name or "").strip().lstrip("@").lower()


def _backfill_people(con):
    sources = [("threads", "SELECT username, their_text, our_reply, answered_at FROM answered")]
    for t, plat in (("ig_answered", "instagram"), ("yt_answered", "youtube")):
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            sources.append((plat, f"SELECT username, text, reply, at FROM {t}"))
    for plat, q in sources:
        try:
            rows = con.execute(q + " ORDER BY 4").fetchall()
        except Exception:
            continue
        for user, said, replied, at in rows:
            remember(con, plat, user, said or "", replied or "", at=at, commit=False, notes=False)
    con.commit()


def remember(con, platform, user, said, replied, at=None, commit=True, notes=True, vibe=None):
    """Call after every reply on every platform. the creator never forgets a commenter."""
    who = _who(user)
    if not who or who == "someone":
        return
    at = at or now().isoformat()
    r = con.execute("SELECT platforms, first_seen, n, recent, note, note_n FROM people WHERE who=?", (who,)).fetchone()
    plats, first, n, recent, note, note_n = (json.loads(r[0]), r[1], r[2], json.loads(r[3]), r[4], r[5]) if r else \
        ([], at, 0, [], "", 0)
    if platform not in plats:
        plats.append(platform)
    recent = (recent + [{"p": platform, "said": said[:200], "you": (replied or "")[:160], "at": at[:10],
                         **({"vibe": vibe} if vibe else {})}])[-6:]
    n += 1
    con.execute("INSERT OR REPLACE INTO people VALUES (?,?,?,?,?,?,?,?,?)",
                (who, user.lstrip("@"), json.dumps(plats), first, at, n, json.dumps(recent), note, note_n))
    if commit:
        con.commit()
    if notes and n >= 2 and (n in (2, 4, 7) or n % 10 == 0) and n != note_n:
        _update_note(con, who)


def _update_note(con, who):
    import threadsbot as tb
    r = con.execute("SELECT name, n, recent, note FROM people WHERE who=?", (who,)).fetchone()
    if not r:
        return
    convo = "\n".join(f"- ({x['p']}) they said: {x['said']} | you said: {x['you']}" for x in json.loads(r[2]))
    raw = tb.ask(f"You keep notes on the fans of [CREATOR], a gaming/comedy creator. Fan: @{r[0]} ({r[1]} comments so far).\n"
                 f"Old note: {r[3] or '(none)'}\nTheir latest comments:\n{convo}\n\nWrite an updated note (max 2 short lines): "
                 "what they're like, what they joke about, their games or inside jokes with [CREATOR], a nickname if one formed. "
                 "Only facts from the comments. No guesses about their age, real identity, health or anything personal. "
                 "Output only the note.", temperature=0.3)
    note = re.sub(r"\s+", " ", (raw or "")).strip()[:300]
    if note:
        con.execute("UPDATE people SET note=?, note_n=n WHERE who=?", (note, who))
        con.commit()


def person_context(con, user):
    """Text for the reply prompt: who this is to the creator."""
    ensure(con)
    r = con.execute("SELECT platforms, first_seen, n, recent, note FROM people WHERE who=?", (_who(user),)).fetchone()
    if not r:
        return ("\n\nWHO THIS IS: a brand-new commenter, their first time ever. Welcome them warmly (like a new member of "
                "the community) so they come back.")
    plats, first, n, recent, note = json.loads(r[0]), r[1], r[2], json.loads(r[3]), r[4]
    last = recent[-1] if recent else None
    tier = "a REGULAR (treat them like a friend: call back to old jokes)" if n >= 5 else \
        ("a returning fan (show you remember them)" if n >= 2 else "someone who commented once before")
    txt = (f"\n\nWHO THIS IS: @{user.lstrip('@')} is {tier}. {n} comment(s) since {first[:10]} on "
           f"{', '.join(plats)}.")
    if note:
        txt += f" What you know about them: {note}"
    vibes = [x.get("vibe") for x in recent if x.get("vibe")]
    if vibes:
        from collections import Counter
        txt += " Their usual vibe: " + ", ".join(f"{v} ({k})" for v, k in Counter(vibes).most_common(2)) + "."
    if last:
        txt += f" Last time ({last['p']}) they said \"{last['said'][:120]}\" and you said \"{last['you'][:100]}\"."
    return txt


def regulars(con, k=5):
    ensure(con)
    rows = con.execute("SELECT name, n, platforms FROM people WHERE n>=3 ORDER BY n DESC, last_seen DESC LIMIT ?",
                       (k,)).fetchall()
    total = con.execute("SELECT COUNT(*), COALESCE(SUM(n>=2),0) FROM people").fetchone()
    if not total[0]:
        return ""
    line = f"## YOUR PEOPLE\n- {total[0]} people have commented, {total[1]} came back more than once (that's real growth)."
    if rows:
        line += "\n- Top regulars: " + ", ".join(f"@{n} ({c})" for n, c, _ in rows) + " (shout them out sometimes)"
    return line


# ======================= archive + lessons (memory stream) =======================
def _words(t):
    return {w for w in re.findall(r"[a-z0-9']{3,}", (t or "").lower()) if w not in STOP}


def _overlap(a, b):
    return len(a & b) / (math.sqrt(len(a) * len(b)) or 1)


# ---- meaning-based search: a small local embedding model in Ollama (falls back to keyword matching) ----
EMBED_MODEL = os.environ.get("BOT_EMBED_MODEL", "nomic-embed-text")
_EMBED = {"ok": None}


def _embed(con, texts):
    """Vectors for texts (cached in the database forever). None if the embedding model isn't available."""
    import hashlib
    from array import array
    import requests
    if _EMBED["ok"] is False or not texts:
        return None
    con.execute("CREATE TABLE IF NOT EXISTS brain_vec (h TEXT PRIMARY KEY, v BLOB)")
    keys = [hashlib.sha1(t.encode("utf-8", "ignore")).hexdigest() for t in texts]
    have = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        have.update(con.execute(f"SELECT h, v FROM brain_vec WHERE h IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    todo = [(k, t) for k, t in zip(keys, texts) if k not in have]
    try:
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            r = requests.post("http://localhost:11434/api/embed", timeout=120,
                              json={"model": EMBED_MODEL, "input": [t[:2000] for _, t in batch], "keep_alive": "2m"})
            if r.status_code == 404 and _EMBED["ok"] is None:  # not downloaded yet: get it once (~270 MB)
                print(f"[memory] downloading the small search model {EMBED_MODEL} (one time)...", flush=True)
                requests.post("http://localhost:11434/api/pull", json={"model": EMBED_MODEL, "stream": False}, timeout=1800)
                r = requests.post("http://localhost:11434/api/embed", timeout=120,
                                  json={"model": EMBED_MODEL, "input": [t[:2000] for _, t in batch], "keep_alive": "2m"})
            r.raise_for_status()
            for (k, _), v in zip(batch, r.json()["embeddings"]):
                b = array("f", v).tobytes()
                have[k] = b
                con.execute("INSERT OR REPLACE INTO brain_vec VALUES (?,?)", (k, b))
        con.commit()
        _EMBED["ok"] = True
    except Exception as e:
        if _EMBED["ok"] is None:
            print(f"[memory] meaning-based search unavailable ({type(e).__name__}); using keyword matching")
        _EMBED["ok"] = False
        return None
    out = []
    for k in keys:
        a = array("f")
        a.frombytes(have[k])
        out.append(a)
    return out


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1
    nb = math.sqrt(sum(y * y for y in b)) or 1
    return dot / (na * nb)


def _similarity(con, about, texts):
    """0..1 similarity of `about` to each text: by meaning when possible, else by shared words."""
    vecs = _embed(con, [about] + list(texts)) if about.strip() else None
    if vecs:
        q = vecs[0]
        return [max(0.0, (_cos(q, v) - 0.45) / 0.45) for v in vecs[1:]]  # nomic: ~0.45 unrelated, ~0.9 same idea
    qw = _words(about)
    return [_overlap(qw, _words(t)) if qw else 0 for t in texts]


def recall_posts(con, about, k=3, exclude_recent_hours=0):
    """Past posts (every platform) most like `about`, with how they did vs usual."""
    if not about.strip():
        return []
    rows = [("threads", t, r, s) for t, r, s in con.execute(
        "SELECT text, reward, score FROM posts WHERE deleted=0 AND text IS NOT NULL ORDER BY id DESC LIMIT 300")]
    rows += [(p, t, r, s) for p, t, r, s in con.execute(
        "SELECT platform, title, reward, score FROM mm_posts WHERE title IS NOT NULL ORDER BY id DESC LIMIT 300")]
    sims = _similarity(con, about, [t or "" for _, t, _, _ in rows])
    scored = sorted(zip(sims, rows), key=lambda x: -x[0])
    return [row for o, row in scored[:k] if o >= 0.2]


def recall(con, about, platform=None, k_lessons=5):
    """The 'you remember' block: relevant lessons + similar past posts. Goes near the end of a prompt."""
    ensure(con)
    out = []
    rows = con.execute("SELECT id, at, platform, text, importance FROM brain_lessons ORDER BY id DESC LIMIT 400").fetchall()
    if rows:
        sims = _similarity(con, about, [r[3] for r in rows])

        def rank(i):
            r = rows[i]
            days = max((now() - datetime.fromisoformat(r[1])).days, 0)
            plat = 0.15 if platform and r[2] and r[2].lower() in (platform.lower(), "all") else 0
            return 0.5 * sims[i] + 0.3 * (r[4] or 5) / 10 + 0.2 * (0.98 ** days) + plat
        best = [rows[i] for i in sorted(range(len(rows)), key=rank, reverse=True)[:k_lessons]]
        out.append("LESSONS YOU REMEMBER:\n" + "\n".join(f"- [{r[2] or 'all'}] {r[3]}" for r in best))
        con.executemany("UPDATE brain_lessons SET uses=uses+1 WHERE id=?", [(r[0],) for r in best])
    past = recall_posts(con, about)
    if past:
        def how(r):
            return "not judged yet" if r is None else ("did BETTER than usual" if r > 0.15 else
                                                        ("did WORSE than usual" if r < -0.15 else "did about usual"))
        out.append("SIMILAR THINGS YOU POSTED BEFORE (don't repeat yourself; build on what worked):\n" +
                   "\n".join(f"- ({p}) \"{(t or '')[:110]}\" -> {how(r)}" for p, t, r, s in past))
    return ("\n\n" + "\n\n".join(out)) if out else ""


def add_lessons(con, items):
    """items: [(platform, text, importance)]. Near-duplicates strengthen the old lesson instead of piling up."""
    ensure(con)
    existing = con.execute("SELECT id, text, importance FROM brain_lessons ORDER BY id DESC LIMIT 500").fetchall()
    added = 0
    for plat, text, imp in items:
        text = re.sub(r"\s+", " ", text or "").strip()
        if len(text) < 12:
            continue
        w = _words(text)
        dup = next((e for e in existing if _overlap(w, _words(e[1])) > 0.6), None)
        if dup:
            con.execute("UPDATE brain_lessons SET importance=MIN(10, importance+1), at=? WHERE id=?",
                        (now().isoformat(), dup[0]))
        else:
            con.execute("INSERT INTO brain_lessons (at, platform, text, importance) VALUES (?,?,?,?)",
                        (now().isoformat(), plat, text[:300], float(max(1, min(10, imp or 5)))))
            added += 1
    con.commit()
    return added


def lessons_from_settled(con, done):
    """One small model call per batch of settled posts -> a few new specific lessons for the memory stream."""
    import threadsbot as tb
    if not done:
        return
    lines = "\n".join(f"- ({p}, {k}) {'BEAT' if a > 0.15 else ('MISSED' if a < -0.15 else 'matched')} your usual "
                      f"({a:+.2f}): {(t or '')[:160]}" for p, k, t, s, a, _ in done[:15])
    raw = tb.ask(f"{tb.IDENTITY[:600]}\n\nThese posts just got judged against your own usual results:\n{lines}\n\n"
                 "Write 1-4 SHORT, SPECIFIC lessons (what to do more of, or less of, and where). Rate how important "
                 "each one is (1-10). Reply ONLY a JSON list: [{\"platform\": \"threads|youtube|tiktok|instagram|all\", "
                 "\"lesson\": \"...\", \"importance\": 7}]", temperature=0.3)
    try:
        data = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
        n = add_lessons(con, [(d.get("platform", "all"), d.get("lesson", ""), d.get("importance", 5))
                              for d in data if isinstance(d, dict)])
        if n:
            print(f"[brain] learned {n} new lesson(s)")
    except Exception:
        pass


def tick(con):
    """Run every cycle: settle results, learn lessons from them."""
    import threadsbot as tb
    done = settle(con)
    if done:
        lessons_from_settled(con, done)
    try:
        import mm_content  # winners -> remix ideas, best fan comments -> content ideas, new series
        mm_content.tick(con, tb.ask, done)
    except Exception as e:
        print(f"[content ideas skipped] {e}")
    return done


def watch_summary(con):
    """How long people actually watch, per platform and per clip style (length, hook, captions, layout)."""
    try:
        rows = con.execute("SELECT p.platform, p.retention, p.skip_rate, c.variant FROM mm_posts p LEFT JOIN mm_clips c "
                           "ON c.id=p.clip_id WHERE p.retention IS NOT NULL AND p.posted_at > ?",
                           ((now() - timedelta(days=30)).isoformat(),)).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    out = ["## WATCH TIME (last 30 days; higher % watched = the algorithm pushes it more)"]
    by_plat, by_style = {}, {}
    for plat, ret, skip, var in rows:
        by_plat.setdefault(plat, []).append((ret, skip))
        try:
            v = json.loads(var or "{}")
        except Exception:
            v = {}
        for k in ("length", "hook", "captions", "layout_used", "zoom"):
            if k in v:
                by_style.setdefault(f"{k}={v[k]}", []).append(ret)
    for plat, xs in by_plat.items():
        skips = [s for _, s in xs if s is not None]
        out.append(f"- {plat}: people watch {sum(r for r, _ in xs) / len(xs):.0f}% of a clip on average ({len(xs)} clips)"
                   + (f", {sum(skips) / len(skips):.0f}% swipe away in the first 3 seconds" if skips else ""))
    styles = sorted(((sum(v) / len(v), k, len(v)) for k, v in by_style.items() if len(v) >= 2), reverse=True)
    if styles:
        out.append("- by clip style: " + ", ".join(f"{k} {a:.0f}% ({n})" for a, k, n in styles[:8]))
    return "\n".join(out)


def snapshot_for_prompts(con):
    """Stable block (same all run long, so Ollama can reuse its cache): mood, what's working, your people."""
    try:
        ensure(con)
        return "\n\n".join(x for x in (mood_text(con), arm_summary(con), watch_summary(con), regulars(con)) if x)
    except Exception as e:
        return f"(brain unavailable: {e})"


def report(con):
    """Plain-text overview for the app."""
    ensure(con)
    parts = [snapshot_for_prompts(con)]
    rows = con.execute("SELECT at, dim, reason FROM brain_decisions ORDER BY id DESC LIMIT 12").fetchall()
    if rows:
        parts.append("## LAST DECISIONS\n" + "\n".join(f"- {a[5:16]} {d}: {r}" for a, d, r in rows))
    ls = con.execute("SELECT platform, text, importance FROM brain_lessons ORDER BY importance DESC, id DESC LIMIT 15").fetchall()
    if ls:
        n = con.execute("SELECT COUNT(*) FROM brain_lessons").fetchone()[0]
        parts.append(f"## TOP LESSONS ({n} total)\n" + "\n".join(f"- [{p}] ({i:.0f}) {t}" for p, t, i in ls))
    ppl = con.execute("SELECT name, n, platforms, note FROM people ORDER BY n DESC, last_seen DESC LIMIT 25").fetchall()
    if ppl:
        parts.append("## PEOPLE IT REMEMBERS (top 25)\n" + "\n".join(
            f"- @{nm}: {n} comments on {', '.join(json.loads(p))}" + (f" | {nt}" if nt else "") for nm, n, p, nt in ppl))
    return "\n\n".join(parts)
