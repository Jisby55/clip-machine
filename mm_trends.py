"""
Trend radar: what's hot in gaming right now, refreshed every 3 hours.
Sources: YouTube's trending Gaming videos (US) + Google Trends daily searches filtered to gaming/creators.
The bot TRIES trends right away (new 'trend' post type + trend angles on clips/pictures) and keeps or drops
them based on how they score, while still mixing in its own the creator stuff.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests


MEME_FEEDS = ("https://knowyourmeme.com/newsfeed.rss", "https://www.reddit.com/r/gamingmemes/top/.rss?t=day",
              "https://www.reddit.com/r/memes/top/.rss?t=day")


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS trends (topic TEXT PRIMARY KEY, source TEXT, found_at TEXT,
                   used INTEGER DEFAULT 0)""")
    con.commit()


def refresh(con, force=False):
    import threadsbot as tb
    ensure(con)
    row = con.execute("SELECT value FROM meta WHERE key='trends_at'").fetchone()
    if row and not force and datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) < timedelta(hours=3):
        return 0
    found = []
    try:  # YouTube trending gaming
        import mm_youtube
        r = mm_youtube.service().videos().list(part="snippet", chart="mostPopular", videoCategoryId="20",
                                               regionCode="US", maxResults=15).execute()
        found += [("youtube_gaming", it["snippet"]["title"]) for it in r.get("items", [])]
    except (SystemExit, Exception) as e:
        print(f"[trends: YouTube trending unavailable] " + ("YouTube isn't connected" if isinstance(e, SystemExit) else type(e).__name__))
    try:  # Google Trends daily searches: gaming/creator ones, plus fun pop-culture ones for the trending mode
        x = requests.get("https://trends.google.com/trending/rss?geo=US", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 ClipMachine"}).text
        for item in ET.fromstring(x).iter("item"):
            t = (item.findtext("title") or "").strip()
            news = " ".join((n.text or "") for n in item.iter() if n.tag.endswith("news_item_title"))
            if not t:
                continue
            if tb.CREATOR.search(t + " " + news):
                found.append(("google_trends", t))
            elif _fun(t + " " + news):
                found.append(("pop", f"{t} ({news[:90]})" if news else t))
    except Exception as e:
        print(f"[trends: Google Trends unavailable] {type(e).__name__}")
    try:  # what's popular on YouTube right now, any category
        import mm_youtube
        r = mm_youtube.service().videos().list(part="snippet", chart="mostPopular", regionCode="US",
                                               maxResults=25).execute()
        for it in r.get("items", []):
            t = it["snippet"]["title"]
            if _fun(t + " " + it["snippet"].get("description", "")[:200]):
                found.append(("pop", t))
    except (SystemExit, Exception) as e:
        print(f"[trends: YouTube popular unavailable] " + ("YouTube isn't connected" if isinstance(e, SystemExit) else type(e).__name__))
    import mm_words
    for url in MEME_FEEDS:  # popular memes right now (best effort; any feed that's down is just skipped)
        try:
            x = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                                     "ClipMachine/1.0"}).text
            titles = re.findall(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", x, re.S)[1:13]
            for t in titles:
                t = re.sub(r"&amp;", "&", re.sub(r"&#?\w+;", " ", re.sub(r"&amp;", "&", t))).strip()
                if 8 <= len(t) <= 140 and not mm_words.is_sexual(t) and not mm_words.AVOID.search(t):
                    found.append(("meme", t))
        except Exception as e:
            print(f"[trends: meme feed unavailable] {type(e).__name__}")
    new = 0
    now = datetime.now(timezone.utc).isoformat()
    for src, topic in found:
        topic = re.sub(r"\s+", " ", topic).strip()[:150]
        if not topic or tb.TRAGEDY.search(topic):  # never chase tragedies
            continue
        cur = con.execute("INSERT OR IGNORE INTO trends (topic, source, found_at) VALUES (?,?,?)", (topic, src, now))
        new += cur.rowcount
    con.execute("INSERT OR REPLACE INTO meta VALUES ('trends_at', ?)", (now,))
    con.commit()
    print(f"Trend radar: {len(found)} trending topics checked, {new} new")
    return new


def _fun(text):
    """A trend that's OK to joke about: not news about tragedy, crime, politics, health, money, or anything sexual."""
    import mm_words
    import threadsbot as tb
    return not (tb.TRAGEDY.search(text) or mm_words.AVOID.search(text) or mm_words.is_sexual(text) or
                POLITICS.search(text))


POLITICS = re.compile(r"\b(president|senat\w*|congress|governor|mayor|vote|voting|ballot|campaign|democrat\w*|"
                      r"republican\w*|gop|white house|supreme court|court|judge|lawsuit|tariff\w*|immigra\w*|ice\b|"
                      r"police|shutdown|stocks?|dow|nasdaq|fed\b|inflation|israel|gaza|ukraine|russia|china|iran|"
                      r"protest\w*|trial|verdict|sentenc\w*|pope|church|religio\w*)\b", re.I)


def fresh(con, hours=24, limit=8, memes=False, pop=False):
    """Recent trends the bot hasn't used yet (newest first). memes=True: popular memes. pop=True: anything popular
    (not just gaming), for the 'trending' content mode."""
    ensure(con)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    if pop:
        return [r[0] for r in con.execute("SELECT topic FROM trends WHERE used=0 AND found_at > ? AND source='pop' "
                                          "ORDER BY found_at DESC, RANDOM() LIMIT ?", (since, limit))]
    return [r[0] for r in con.execute(
        "SELECT topic FROM trends WHERE used=0 AND found_at > ? AND "
        + ("source='meme' " if memes else "source NOT IN ('meme', 'pop') ")
        + "ORDER BY found_at DESC, RANDOM() LIMIT ?", (since, limit))]


def mark_used(con, topic):
    con.execute("UPDATE trends SET used=1 WHERE topic=?", (topic,))
    con.commit()


def should_try(con, platform, base=0.4):
    """How often to put a trend angle on a post: starts at 40%, goes up if trend posts beat normal ones on
    this platform, down if they flop (but never to zero, so it keeps testing)."""
    import random
    rows = con.execute("SELECT CASE WHEN kind LIKE 'trend%' OR kind LIKE '%_trend' THEN 1 ELSE 0 END, AVG(score), COUNT(*) "
                       "FROM mm_posts WHERE platform=? AND score IS NOT NULL GROUP BY 1", (platform,)).fetchall()
    stats = {r[0]: (r[1], r[2]) for r in rows}
    p = base
    if 1 in stats and 0 in stats and stats[1][1] >= 3 and stats[0][1] >= 3:
        p = 0.65 if stats[1][0] > stats[0][0] else 0.2
    return random.random() < p
