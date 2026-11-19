"""
Phone notifications through ntfy (free app: iPhone App Store / Google Play, search "ntfy").
In the app: + -> subscribe to the topic shown in machine_config.json "ntfy_topic" (server ntfy.sh).
The topic name is random, so only you get these.
"""
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone

import requests

import mm_safety
import creator_profile as profile  # noqa: E402

SERVER = "https://ntfy.sh"


def topic():
    """The private topic name; created once and saved to machine_config.json."""
    cfg = mm_safety.config()
    t = cfg.get("ntfy_topic")
    if not t:
        t = "clip-machine-" + secrets.token_hex(5)
        mm_safety.save_setting("ntfy_topic", t)
    return t


def send(title, message, priority=3, tags=None, con=None, dedupe_hours=0, click=None):
    """Push to the phone. dedupe_hours>0: the same title+message is only sent once in that window."""
    import creator_profile as profile
    title, message = profile.fill(title), profile.fill(message)
    if not mm_safety.config().get("alerts_enabled", True):
        return False
    if con is not None and dedupe_hours:
        key = "ntfy_" + hashlib.md5((title + message).encode()).hexdigest()[:16]
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row and time.time() - float(row[0]) < dedupe_hours * 3600:
            return False
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(time.time())))
        con.commit()
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    last = None
    for attempt in range(3):  # retry: a dropped connection used to lose the message silently
        try:
            r = requests.post(f"{SERVER}/{topic()}", data=message.encode("utf-8"), headers=headers, timeout=20)
            if r.status_code < 300:
                return True
            last = f"ntfy said {r.status_code}: {r.text[:150]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:200]
        time.sleep(3 * (attempt + 1))
    print(f"[phone alert NOT sent ({title}): {last}]")
    return False


def send_file(title, filename, content, message="", tags=None, priority=4):
    """Push a small file (e.g. the TikTok caption as a .txt) to the phone as an ntfy attachment."""
    if not mm_safety.config().get("alerts_enabled", True):
        return False
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Filename": filename,
               "Priority": str(priority)}
    if message:  # header text must be plain ASCII
        headers["Message"] = message.encode("ascii", "ignore").decode()[:300]
    if tags:
        headers["Tags"] = ",".join(tags)
    data = content.encode("utf-8") if isinstance(content, str) else content
    for attempt in range(3):
        try:
            r = requests.put(f"{SERVER}/{topic()}", data=data, headers=headers, timeout=30)
            if r.status_code < 300:
                return True
            print(f"[phone file not sent: ntfy said {r.status_code}: {r.text[:150]}]")
        except Exception as e:
            print(f"[phone file not sent: {type(e).__name__}]")
        time.sleep(3 * (attempt + 1))
    return False


def posted(platform, title, link=None, extra=""):
    """'A post just went out' notification (one per post, every platform)."""
    send(f"Posted on {platform}", (f"{title}" + (f"\n{extra}" if extra else "")).strip()[:900], priority=2,
         tags=["white_check_mark"], click=link)


def alert(con, what, detail):
    """Something broke. Same problem is only pushed once every 6 hours."""
    send(f"Clip Machine: {what} problem", str(detail)[:400], priority=4, tags=["warning"], con=con, dedupe_hours=6)
