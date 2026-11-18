"""
Instagram DMs: the bot answers people who message your account (checked every 5 minutes with the comment replies).

Rules it follows:
- Only answers people who messaged first, and only within Instagram's 24-hour window.
- The first reply in each chat says it's the creator's AI (the owner reads them too).
- Never sends links, never shares personal info, never agrees to deals/money/meetups (says the owner will see it).
- Messages that look like spam, scams, something sexual, threats or someone in a bad place get NO auto-reply:
  the owner gets a phone alert instead.
- At most 6 auto-replies per person per day, so two bots can't ping-pong forever.

Needs the instagram_manage_messages permission (Facebook login) or instagram_business_manage_messages (Instagram
login) on the Meta app, and "Allow access to messages" ON in the Instagram app (Settings -> Messages and story
replies -> Message controls -> Connected tools). `python machine.py dm-check` tells you what's missing.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import mm_instagram
import creator_profile as profile  # noqa: E402

SPAM = re.compile(r"(https?://|www\.|\.com\b|promo|promot(e|ion)|collab\w*|sponsor|shoutout for|dm for|crypto|"
                  r"bitcoin|forex|invest|cash ?app|paypal|gift ?card|giveaway|onlyfans|followers? for|buy followers|"
                  r"brand ambassador|ambassador|verify your account|copyright (strike|violation)|meta support|"
                  r"your account (will|has been))", re.I)
HANDOFF = re.compile(r"\b(kill (myself|me)|suicid\w*|self[- ]?harm|cut(ting)? myself|want to die|end it all|"
                     r"abuse|threat\w*|police|lawyer|lawsuit|nudes?|sex\w*|naked|address|where do you live|phone number|"
                     r"how old are you|meet up|meet irl)\b", re.I)
MAX_PER_DAY = 6


def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS ig_dms (msg_id TEXT PRIMARY KEY, convo TEXT, user TEXT, text TEXT,
                   reply TEXT, at TEXT, status TEXT)""")
    con.commit()


def _me():
    tok = mm_instagram._load()
    return str(tok.get("ig_id") or ""), (tok.get("ig_username") or profile.get("instagram_username") or "").lower()


def conversations(limit=20):
    r = mm_instagram._get("me/conversations", platform="instagram", limit=limit,
                          fields="id,updated_time,participants,messages.limit(8){id,message,from,created_time}")
    return r.get("data", [])


def send(user_id, text):
    return mm_instagram._post("me/messages", recipient=json.dumps({"id": user_id}),
                              message=json.dumps({"text": text}), messaging_type="RESPONSE")


def check():
    """Can the bot read and answer DMs? Prints what to fix if not."""
    try:
        mm_instagram.whoami()
        data = conversations(limit=3)
    except Exception as e:
        msg = str(e)
        print(f"Instagram DMs: NOT working yet.\n  Meta said: {msg}\n")
        if re.search(r"permission|scope|\(#10\)|\(#3\)|\(#200\)|access", msg, re.I):
            print("  Fix: the Meta app needs the messaging permission, then a NEW Instagram token.\n"
                  "   1. developers.facebook.com -> your app -> Use cases -> Instagram -> Permissions -> add "
                  "'instagram_manage_messages' (or 'instagram_business_manage_messages').\n"
                  "   2. Make the token again with that permission ticked and put it in secrets.env (INSTAGRAM_TOKEN=...).\n"
                  "   3. In the Instagram app: Settings -> Messages and story replies -> Message controls -> "
                  "Connected tools -> Allow access to messages: ON.")
        return False
    print(f"Instagram DMs: working. Can see {len(data)} recent chat(s).")
    return True


def _alert(user, text, why):
    try:
        import mm_notify
        mm_notify.send(f"Instagram DM from @{user} needs YOU", f"{text[:300]}\n\n(Not auto-answered: {why})",
                       priority=4, tags=["envelope"], click="https://www.instagram.com/direct/inbox/")
    except Exception:
        pass


def _write(history, user, first):
    import mm_words
    import threadsbot as tb
    raw = tb.ask(
        f"{tb.IDENTITY}\n\nSomeone sent you ([CREATOR], @[HANDLE] on Instagram) a DM. Here is the chat, oldest first:\n"
        + "\n".join(history) +
        "\n\nWrite your reply to their LAST message: 1-2 short sentences, friendly and funny, like texting a fan you "
        "like. Answer what they actually said. Rules: no links, never share personal info, never agree to deals, money, "
        "sponsorships, collabs or meeting up (say [OWNER] will see it and get back to them if it's real), no lore "
        "characters unless they bring them up, don't ask them to like/follow/share anything. Reply with ONLY the "
        "message text.", temperature=0.8, max_tokens=120)
    text = mm_words.shorten(mm_words.clean(tb.tidy(raw or ""), "instagram", "reply"), 220, 2)
    if not text:
        return None, "the AI wrote nothing usable"
    if re.search(r"https?://|www\.", text):
        return None, "the reply had a link"
    ok, why = tb.safety_verdict(text)
    if not ok:
        return None, f"safety check: {why}"
    if first:
        text += profile.fill(" (this is [CREATOR]'s AI, [OWNER] reads these too)")
    return text, None


def tick(con, live=None):
    """Answer new DMs. Returns how many were answered."""
    import mm_safety
    if not mm_safety.config().get("instagram_dms", True):
        return 0
    ensure(con)
    live = (os.environ.get("DRY_RUN", "1") == "0") if live is None else live
    try:
        my_id, my_name = _me()
        convos = conversations()
    except Exception as e:
        if not getattr(tick, "_said", False):
            print(f"Instagram DMs: not available yet ({str(e)[:160]}). Run 'python machine.py dm-check' for the fix.")
            tick._said = True
        return 0
    now = datetime.now(timezone.utc)
    done = 0
    for cv in convos:
        msgs = (cv.get("messages") or {}).get("data", [])
        if not msgs:
            continue
        last = msgs[0]  # newest first
        frm = last.get("from") or {}
        user = (frm.get("username") or "").lower()
        uid = str(frm.get("id") or "")
        if not uid or uid == my_id or user == my_name:
            continue  # we spoke last
        if con.execute("SELECT 1 FROM ig_dms WHERE msg_id=?", (last["id"],)).fetchone():
            continue
        text = (last.get("message") or "").strip()
        try:
            at = datetime.fromisoformat(last["created_time"].replace("+0000", "+00:00"))
        except Exception:
            at = now
        if now - at > timedelta(hours=23):
            con.execute("INSERT OR IGNORE INTO ig_dms VALUES (?,?,?,?,?,?,?)",
                        (last["id"], cv["id"], user, text, None, now.isoformat(), "too_old"))
            continue
        if not text:  # a sticker, a reel share, a photo: nothing to answer in words
            con.execute("INSERT OR IGNORE INTO ig_dms VALUES (?,?,?,?,?,?,?)",
                        (last["id"], cv["id"], user, "", None, now.isoformat(), "no_text"))
            continue
        since = (now - timedelta(days=1)).isoformat()
        sent_today = con.execute("SELECT COUNT(*) FROM ig_dms WHERE convo=? AND status='sent' AND at > ?",
                                 (cv["id"], since)).fetchone()[0]
        status, reply = None, None
        if SPAM.search(text):
            status = "spam"
        elif HANDOFF.search(text):
            status = "for_owner"
            _alert(user, text, "it looks like something you should answer yourself")
        elif sent_today >= MAX_PER_DAY:
            status = "daily_cap"
        else:
            first = not con.execute("SELECT 1 FROM ig_dms WHERE convo=? AND status='sent'", (cv["id"],)).fetchone()
            history = []
            for m in reversed(msgs[:8]):
                who = "you" if str((m.get("from") or {}).get("id")) == my_id else "@" + ((m.get("from") or {}).get("username") or "them")
                if m.get("message"):
                    history.append(f"{who}: {m['message'][:300]}")
            reply, why = _write(history, user, first)
            if not reply:
                print(f"[Instagram DM to @{user} not sent: {why}; trying again next run]")
                continue
            if not live:
                print(f"[DRY RUN] Instagram DM reply to @{user}: {reply}")
                continue
            try:
                send(uid, reply)
            except Exception as e:
                print(f"[Instagram DM to @{user} failed: {str(e)[:200]}]")
                continue
            status = "sent"
            done += 1
            print(f"Instagram DM: answered @{user}: {reply}")
            try:
                import threadsbot as tb
                tb.remember_person("instagram", user, text, reply)
            except Exception:
                pass
        con.execute("INSERT OR IGNORE INTO ig_dms VALUES (?,?,?,?,?,?,?)",
                    (last["id"], cv["id"], user, text, reply, now.isoformat(), status))
        con.commit()
    con.commit()
    return done
