"""
Outreach: the bot joins conversations OUTSIDE its own posts, carefully, and learns from it.

THREADS (official API, needs threads_keyword_search + threads_manage_mentions on the token):
  - A few times a day it searches a topic it cares about (from profile.json outreach_topics), reads the top posts,
    picks ONE worth joining and leaves a short, funny, genuinely on-topic reply. No links, no "check out my
    channel", no begging. Default: max 6 a day, 75+ minutes apart, 9am-11pm. One reply per author per week.
  - While reading the top posts it writes down ONE growth lesson (why those posts work) into its memory.
  - 24h later it checks how its reply did (views, likes, replies) and learns which topics and reply styles work
    (same discounted-Thompson brain as everything else).
  - It answers every @mention of your account.
INSTAGRAM (official API): replies when someone tags your account in their post. Commenting on strangers' posts isn't
  allowed by Instagram's API, so it doesn't.
Settings live in machine_config.json -> "outreach".
"""
import json
import re
from datetime import datetime, timedelta, timezone

import requests

import creator_profile as profile

API = "https://graph.threads.net/v1.0"
TOPICS = list(profile.get("outreach_topics") or ["gaming memes", "PC gaming", "indie games", "gamer humor"])
STYLES = ["joke", "hot_take", "genuine", "question", "lore_callback"]
STYLE_HELP = {"joke": "a quick dumb joke that riffs on their exact post",
              "hot_take": "a confidently wrong but harmless hot take they'll want to argue with",
              "genuine": "a real, friendly reaction (agree, relate, compliment something specific) with your humor",
              "question": "a short curious question about their post that's easy and fun to answer",
              "lore_callback": "tie their post to your lore ([LORE]) in one line, only if it fits"}


def now():
    return datetime.now(timezone.utc)


def cfg():
    import mm_safety
    c = {"enabled": True, "threads_per_day": 6, "min_minutes_between": 75, "hours": [9, 23], "instagram_mentions": True}
    c.update(mm_safety.config().get("outreach", {}))
    return c


def ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS outreach (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, target_id TEXT "
                "UNIQUE, author TEXT, their_text TEXT, topic TEXT, style TEXT, our_reply TEXT, reply_id TEXT, at TEXT, "
                "views INTEGER, likes INTEGER, replies INTEGER, score REAL, settled INTEGER)")
    con.commit()


def _get(path, **params):
    import threadsbot as tb
    params["access_token"] = tb.token()
    r = requests.get(f"{API}/{path}", params=params, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        msg = data.get("error", {}).get("message", r.text[:200])
        raise RuntimeError(f"Threads {path}: {msg}")
    return data


def _bad(text):
    import mm_words
    import threadsbot as tb
    return (not text or len(text) < 20 or "http" in text or tb.too_morbid(text) or bool(mm_words.AVOID.search(text))
            or bool(tb.TRAGEDY.search(text)))


def _me():
    try:
        return _get("me", fields="username").get("username", profile.get("threads_username") or "").lower()
    except Exception:
        return (profile.get("threads_username") or "").lower()


def threads_outreach(con):
    import mm_brain
    import mm_words
    import threadsbot as tb
    c = cfg()
    if not c["enabled"]:
        return
    ensure(con)
    h = datetime.now().hour
    if not c["hours"][0] <= h < c["hours"][1]:
        return
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    done_today = con.execute("SELECT COUNT(*) FROM outreach WHERE platform='threads' AND at > ?",
                             (midnight.isoformat(),)).fetchone()[0]
    if done_today >= c["threads_per_day"]:
        return
    last = con.execute("SELECT MAX(at) FROM outreach WHERE platform='threads'").fetchone()[0]
    if last and now() - datetime.fromisoformat(last) < timedelta(minutes=c["min_minutes_between"]):
        return
    topic = mm_brain.choose(con, "outreach_topic", TOPICS)
    try:
        found = _get("keyword_search", q=topic, search_type="TOP", limit=25,
                     fields="id,text,username,timestamp,permalink,is_reply,is_quote_post").get("data", [])
    except RuntimeError as e:
        if "permission" in str(e).lower() or "scope" in str(e).lower() or "(#10)" in str(e):
            import mm_notify
            mm_notify.send("Clip Machine: Threads needs a new token", "To join other people's Threads posts, make a new "
                           "Threads token (it now includes keyword search + mentions) and put it in secrets.env (or run: python mac/threads-login.py).",
                           tags=["key"], con=con, dedupe_hours=72)
            return print("Threads outreach: waiting for a Threads token with keyword search permission")
        raise
    me = _me()
    week = (now() - timedelta(days=7)).isoformat()
    recent_authors = {r[0] for r in con.execute("SELECT author FROM outreach WHERE at > ?", (week,))}
    done_ids = {r[0] for r in con.execute("SELECT target_id FROM outreach")}
    cands = []
    for p in found:
        user = (p.get("username") or "").lower()
        ts = p.get("timestamp", "")
        try:
            fresh = now() - datetime.fromisoformat(ts.replace("+0000", "+00:00")) < timedelta(days=3)
        except Exception:
            fresh = True
        if (user and user != me and user not in recent_authors and p["id"] not in done_ids and not p.get("is_reply")
                and fresh and not _bad(p.get("text"))):
            cands.append(p)
    if not cands:
        return print(f"Threads outreach: nothing good to join for '{topic}' right now")
    style = mm_brain.choose(con, "outreach_style", STYLES)
    listing = "\n".join(f"{i}. @{p['username']}: {p['text'][:280]}" for i, p in enumerate(cands[:8]))
    raw = tb.ask(
        f"{tb.IDENTITY}\n\nYOUR MEMORY:\n{tb.read_memory()}{tb.recall(topic, 'threads')}\n\n"
        f"You're browsing the TOP Threads posts about \"{topic}\" right now:\n{listing}\n\n"
        "1) Pick the ONE post where a reply from you would be welcome and funny (skip anything serious, sad, political "
        f"or mean). 2) Write a reply in this style: {STYLE_HELP[style]}. Rules: max 200 characters, talk to THEM about "
        "THEIR post, no links, never mention your channel/videos/account, never ask for likes or follows, don't be "
        "needy or desperate: be the funny confident guy in the replies. 3) Study why these top posts work and write "
        "ONE short, specific growth lesson you can use in your own posts.\n"
        "Reply ONLY JSON: {\"pick\": 0, \"reply\": \"...\", \"lesson\": \"...\"}", temperature=0.8)
    try:
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        target = cands[min(max(int(d.get("pick", 0)), 0), len(cands[:8]) - 1)]
    except Exception:
        return print("Threads outreach: the AI's answer wasn't usable; skipping this time")
    if d.get("lesson"):
        mm_brain.add_lessons(con, [("threads", "From top posts: " + str(d["lesson"])[:250], 6)])
    reply = mm_words.clean(tb.tidy(str(d.get("reply", ""))), "threads", "reply")[:300]
    if not reply or _bad(reply + " " * 20):
        return print("Threads outreach: the reply wasn't safe/usable; skipping")
    ok, why = tb.safety_verdict(reply)
    if not ok:
        return print(f"Threads outreach: reply blocked ({why})")
    if tb.DRY_RUN:
        return print(f"[DRY RUN] Threads outreach reply to @{target['username']} ({topic}/{style}): {reply}")
    try:
        rid = tb.publish(reply, reply_to_id=target["id"])
    except SystemExit as e:
        raise RuntimeError(str(e))
    con.execute("INSERT INTO outreach (platform, target_id, author, their_text, topic, style, our_reply, reply_id, at) "
                "VALUES ('threads',?,?,?,?,?,?,?,?)", (target["id"], target["username"].lower(), target["text"][:500],
                                                       topic, style, reply, rid, now().isoformat()))
    con.commit()
    print(f"Threads outreach: replied to @{target['username']} ({topic}, {style}): {reply}")
    import mm_notify
    mm_notify.posted("Threads (reply on someone else's post)", f"To @{target['username']}: {reply}",
                     target.get("permalink"))


def threads_mentions(con):
    """Answer people who @mention the account on Threads."""
    import threadsbot as tb
    ensure(con)
    try:
        items = _get("me/mentions", fields="id,text,username,timestamp,permalink", limit=25).get("data", [])
    except RuntimeError as e:
        return print(f"Threads mentions: not available yet ({str(e)[:120]})")
    me = _me()
    for m in items:
        if (m.get("username") or "").lower() == me or not m.get("text"):
            continue
        if con.execute("SELECT 1 FROM outreach WHERE target_id=?", (m["id"],)).fetchone():
            continue
        draft, why = tb.write_reply("Threads", "(they mentioned you in their own post)", m.get("username", "someone"),
                                    m["text"])
        if not draft:
            print(f"[Threads mention from @{m.get('username')}: no reply yet ({why})]")
            continue
        if tb.DRY_RUN:
            print(f"[DRY RUN] Threads mention reply to @{m.get('username')}: {draft}")
            continue
        rid = tb.publish(draft, reply_to_id=m["id"])
        tb.remember_person("threads", m.get("username", ""), m["text"], draft)
        con.execute("INSERT INTO outreach (platform, target_id, author, their_text, topic, style, our_reply, reply_id, at) "
                    "VALUES ('threads_mention',?,?,?,?,?,?,?,?)", (m["id"], (m.get("username") or "").lower(),
                                                                   m["text"][:500], "mention", "mention", draft, rid,
                                                                   now().isoformat()))
        con.commit()
        print(f"Threads: answered @{m.get('username')}'s mention: {draft}")


def instagram_mentions(con):
    """Someone tagged your account in their Instagram post -> one friendly comment on it (official mentions API)."""
    import mm_instagram
    import threadsbot as tb
    if not cfg()["instagram_mentions"]:
        return
    ensure(con)
    uid = mm_instagram._uid()
    try:
        items = mm_instagram._get(f"{uid}/tags", fields="id,caption,username,timestamp,permalink", limit=25).get("data", [])
    except Exception as e:
        return print(f"Instagram mentions: not available ({str(e)[:120]})")
    for m in items:
        if con.execute("SELECT 1 FROM outreach WHERE target_id=?", (m["id"],)).fetchone():
            continue
        draft, why = tb.write_reply("Instagram", "(they tagged you in their post)", m.get("username", "someone"),
                                    m.get("caption") or "(a post you were tagged in)")
        if not draft:
            continue
        if tb.DRY_RUN:
            print(f"[DRY RUN] Instagram tag reply to @{m.get('username')}: {draft}")
            continue
        try:
            r = mm_instagram._post(f"{uid}/mentions", media_id=m["id"], message=draft)
        except Exception as e:
            print(f"[Instagram tag reply failed: {str(e)[:150]}]")
            r = {}
        con.execute("INSERT INTO outreach (platform, target_id, author, their_text, topic, style, our_reply, reply_id, at) "
                    "VALUES ('instagram_mention',?,?,?,?,?,?,?,?)", (m["id"], (m.get("username") or "").lower(),
                                                                     (m.get("caption") or "")[:500], "mention", "mention",
                                                                     draft, r.get("id"), now().isoformat()))
        con.commit()
        if r.get("id"):
            tb.remember_person("instagram", m.get("username", ""), m.get("caption") or "", draft)
            print(f"Instagram: commented on @{m.get('username')}'s post you were tagged in: {draft}")


def settle(con):
    """24h after each outreach reply: how did it do? Teach the brain which topics and styles work."""
    import math
    import mm_brain
    import threadsbot as tb
    ensure(con)
    rows = con.execute("SELECT id, reply_id, topic, style FROM outreach WHERE platform='threads' AND settled IS NULL "
                       "AND reply_id IS NOT NULL AND at < ?", ((now() - timedelta(hours=24)).isoformat(),)).fetchall()
    if not rows:
        return
    prev = sorted(math.log1p(s or 0) for (s,) in con.execute(
        "SELECT score FROM outreach WHERE settled=1 AND score IS NOT NULL ORDER BY id DESC LIMIT 20"))
    usual = prev[len(prev) // 2] if prev else 0.0
    for oid, rid, topic, style in rows:
        try:
            m = tb.insights(rid)
        except (SystemExit, Exception):
            m = {}
        s = round(m.get("views", 0) * 0.02 + m.get("likes", 0) * 2 + m.get("replies", 0) * 4 + m.get("reposts", 0) * 3, 2)
        a = max(-1.5, min(1.5, math.log1p(s) - usual))
        mm_brain.learn(con, "outreach_topic", topic, a)
        mm_brain.learn(con, "outreach_style", style, a)
        con.execute("UPDATE outreach SET views=?, likes=?, replies=?, score=?, settled=1 WHERE id=?",
                    (m.get("views", 0), m.get("likes", 0), m.get("replies", 0), s, oid))
    con.commit()
    print(f"Outreach: learned from {len(rows)} reply(s) on other people's posts")


def tick(con):
    settle(con)
    threads_mentions(con)
    threads_outreach(con)
    if cfg()["instagram_mentions"]:
        try:
            instagram_mentions(con)
        except Exception as e:
            print(f"[Instagram mentions skipped] {e}")
