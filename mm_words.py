"""
Platform-safe wording. Everything the bot posts (titles, captions, Threads posts, replies, hashtags) goes through
clean() right before it's published.

What the research found (Nov 2026):
- TikTok: automatic caption checks flag violent/death words (kill, murder, die, dead, suicide), sexual words, drugs
  and alcohol, and weight-shaming terms. Creators swap in "unalive" / "took out" so videos aren't held back.
- YouTube: strong profanity in a TITLE or THUMBNAIL = limited/no ads (bleeped or mild words are fine); game
  violence is fine, but graphic violence in the first 7 seconds or in the thumbnail limits ads.
- Instagram / Threads / Facebook (Meta): posts that beg for likes/comments/shares/tags ("LIKE this if...",
  "comment X", "tag a friend") are DEMOTED (and Pages that do it repeatedly get demoted as a whole; comments too).
  Violent content (people fighting), sexual content and political content aren't recommended to non-followers.
  Threads is cracking down on rage bait.
So: no begging for engagement, no violent/death words, no strong swears in titles, no drugs/sex/politics.
Genuine questions ("which god should I play next?") are fine and are the best way to get comments.
"""
import re

# ---------- violent / death words -> what creators use instead ----------
SWAPS = [
    (r"\bmurder(s|ed|ing)?\b", {"": "unalive", "s": "unalives", "ed": "unalived", "ing": "unaliving"}),
    (r"\bmurderer(s)?\b", {"": "menace", "s": "menaces"}),
    (r"\bkill(s|ed|ing)?\b", {"": "take out", "s": "takes out", "ed": "took out", "ing": "taking out"}),
    (r"\bkiller(s)?\b", {"": "menace", "s": "menaces"}),
    (r"\bsuicid(e|al)\b", {"e": "unalive", "al": "done"}),
    (r"\b(die|dies|died|dying)\b", {"die": "fall", "dies": "falls", "died": "fell", "dying": "falling"}),
    (r"\bdeath(s)?\b", {"": "downfall", "s": "downfalls"}),
    (r"\b(dead|deadass)\b", {"dead": "done", "deadass": "for real"}),
    (r"\b(shoot|shot|shooting|shooter)\b", {"shoot": "blast", "shot": "blasted", "shooting": "blasting", "shooter": "blaster"}),
    (r"\b(gun|guns)\b", {"gun": "blaster", "guns": "blasters"}),
    (r"\b(blood|bloody|gore|gory)\b", {"blood": "chaos", "bloody": "messy", "gore": "chaos", "gory": "messy"}),
    (r"\b(stab|stabbed|stabbing)\b", {"stab": "poke", "stabbed": "poked", "stabbing": "poking"}),
]
I_AM_DEAD = re.compile(r"\b(i'?m|im|i am) (so )?dead\b", re.I)  # "i'm dead" = laughing

PROFANITY = {"fuck": "f*ck", "fucking": "f*cking", "fucked": "f*cked", "motherfucker": "m*therf*cker",
             "shit": "sh*t", "bitch": "b*tch", "bitches": "b*tches", "asshole": "a**hole", "cunt": "c*nt",
             "dick": "d*ck", "pussy": "p*ssy", "cock": "c*ck", "bastard": "b*stard"}

# words that get posts held back or not recommended (drugs, sex, self-harm, politics, scams)
AVOID = re.compile(r"\b(weed|cocaine|coke|meth|heroin|fentanyl|drugs?|high af|stoned|drunk|wasted|vape|vaping|"
                   r"sex|sexy|porn|nude|naked|horny|onlyfans|thot|"
                   r"self[- ]?harm|cutting myself|anorexic|"
                   r"trump|biden|harris|democrat|republican|maga|election|abortion|"
                   r"crypto|bitcoin|get rich|free money|giveaway)\b", re.I)

# engagement bait (Meta demotes it; TikTok's follow-for-follow too)
BAIT = re.compile(
    r"(\b(smash|hit|tap|drop|leave)\b[^.!?\n]{0,25}\b(like|follow|sub(scribe)?|share|comment)s?\b"
    r"|\b(like|share|repost|follow|subscribe|comment|reply|tag)\b[^.!?\n]{0,30}\b(if|for|or|to prove|before|so i|"
    r"if you|this post|this now|below)\b"
    r"|\btag (a|your|ur|3|three|two|2)\b[^.!?\n]{0,25}"
    r"|\b(like|comment|share|follow|repost)\s+(this|it|me)\b"
    r"|\bcomment ['\"“]?\w+['\"”]?\s+(if|for|to)\b"
    r"|\bfollow (for|4) follow\b|\bf4f\b|\bl4l\b|\blike4like\b"
    r"|\b(don'?t|do not) scroll\b|\bscroll(ed)? past\b[^.!?\n]{0,40}"
    r"|\bowe (me|us|the army)\b[^.!?\n]{0,20}\b(reply|like|comment|follow|repost)\b"
    r"|\bdrop (your|ur|a|an|the)\b[^.!?\n]{0,45}\b(below|in the (comments|replies))\b"
    r"|\b(comment|reply|type|say)\s+['\"“‘][^'\"”’\n]{1,24}['\"”’]"
    r"|\bcomment (your|ur|below|now)\b|\breply (below|now)\b|\b(comments?|replies) below\b"
    r"|\breply to (seal|prove|join|confirm|enlist)\b"
    r"|\bif you('re| are)? scroll(ing|ed)?\b[^.!?\n]{0,60}"
    r"|\b(slap|punch|press) (the|that) like\b)", re.I)


def _swap_word(m, table):
    w = m.group(0)
    key = (m.group(1) or "") if m.groups() else ""
    rep = table.get(key.lower(), table.get(w.lower(), next(iter(table.values()))))
    if w.isupper():
        return rep.upper()
    if w[:1].isupper():
        return rep[:1].upper() + rep[1:]
    return rep


PRONOUN_KILL = re.compile(r"\b(kill|kills|killed|killing)\s+(him|her|them|me|you|it|us)\b", re.I)
_TAKE = {"kill": "take", "kills": "takes", "killed": "took", "killing": "taking"}


def soften_violence(text):
    text = I_AM_DEAD.sub(lambda m: m.group(1) + " crying", text)
    text = re.sub(r"\b(kill|kills|killed|killing) it\b",
                  lambda m: {"kill": "crush", "kills": "crushes", "killed": "crushed", "killing": "crushing"}[m.group(1).lower()]
                  + " it", text, flags=re.I)
    text = PRONOUN_KILL.sub(lambda m: f"{_TAKE[m.group(1).lower()]} {m.group(2)} out", text)
    for pat, table in SWAPS:
        text = re.sub(pat, lambda m, t=table: _swap_word(m, t), text, flags=re.I)
    return text


# sexual words: never in titles, captions, hashtags or on-screen text (TikTok/Meta hold these back, YouTube limits ads)
SEXUAL = [
    (r"\b(dildo|dildos|vibrator|vibrators|butt ?plugs?|sex ?toys?|fleshlights?)\b", "toy"),
    (r"\b(penis|penises|dicks?|cocks?|schlong|wiener)\b", "pp"),
    (r"\b(boobs?|boobies|tits|titties|nipples?)\b", "chest"),
    (r"\b(cum|cumming|orgasm\w*|jerk(ing)? off|masturbat\w*|blowjob|handjob)\b", "[REDACTED]"),
    (r"\b(porn\w*|nsfw|hentai|onlyfans|nudes?|naked|horny|sexy|sex)\b", "[REDACTED]"),
]


def desexualize(text):
    for pat, rep in SEXUAL:
        text = re.sub(pat, rep, text, flags=re.I)
    return re.sub(r"(\[REDACTED\]\s*){2,}", "[REDACTED] ", text)


def is_sexual(text):
    return any(re.search(p, text or "", re.I) for p, _ in SEXUAL)


def mask_profanity(text):
    def rep(m):
        w = m.group(0)
        r = PROFANITY[w.lower()]
        return r.upper() if w.isupper() else (r.capitalize() if w[:1].isupper() else r)
    return re.sub(r"\b(" + "|".join(sorted(PROFANITY, key=len, reverse=True)) + r")\b", rep, text, flags=re.I)


def strip_bait(text):
    """Drop the sentences that beg for likes/comments/shares/follows. Keeps genuine questions."""
    parts = re.split(r"(?<=[.!?])\s+|\n", text)
    keep = [p for p in parts if p.strip() and not BAIT.search(p)]
    if not keep:  # the whole thing was begging: nothing worth keeping
        return ""
    return " ".join(keep).strip()


def problems(text):
    """What's still risky (for the safety prompt / logs)."""
    out = []
    if BAIT.search(text or ""):
        out.append("engagement bait")
    if AVOID.search(text or ""):
        out.append(f"avoid-word '{AVOID.search(text).group(0)}'")
    return out


def clean(text, platform="all", kind="caption"):
    """Make text safe for the platform's filters. kind: title | caption | post | reply | hashtag."""
    if not text:
        return text
    t = soften_violence(text)
    t = desexualize(t)
    t = mask_profanity(t)
    if kind != "hashtag":
        t = strip_bait(t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t


def shorten(text, max_chars=110, max_sentences=1):
    """Keep it short: the first sentence(s) that fit. The model likes to ramble; captions under ~80-100 characters
    do best."""
    t = " ".join((text or "").split())
    if len(t) <= max_chars and len(re.findall(r"[.!?]+(?:\s|$)", t)) <= max_sentences:
        return t
    parts = [p for p in re.split(r"(?<=[.!?])\s+", t) if p]
    out = ""
    for p in parts[:max_sentences]:
        if len((out + " " + p).strip()) > max_chars:
            break
        out = (out + " " + p).strip()
    if out:
        return out
    cut = parts[0][:max_chars]
    for sep in (", ", " but ", " and ", " because ", " while "):  # end on a natural break if there is one
        k = cut.rfind(sep)
        if k > max_chars * 0.45:
            return cut[:k].rstrip(" ,;:-")
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:-")


SAFE_TAG = re.compile(r"#?(kill|murder|dead|death|die|suicide|gore|blood|gun|shoot|sex|porn|nsfw|drunk|weed|drugs|dildo|"
                      r"vibrator|toy|penis|dick|cock|boob|tits|cum|nude|naked|horny|redacted)",
                      re.I)


def ok_hashtag(tag):
    return not SAFE_TAG.search(tag or "") and not AVOID.search((tag or "").lstrip("#"))


RULES_FOR_PROMPTS = (
    "PLATFORM-SAFE WORDING (TikTok, YouTube, Instagram, Threads and Facebook filters punish these, so never use them): "
    "1) NEVER beg for engagement: no 'like this', 'like if', 'comment X if', 'share this', 'tag a friend', 'follow for', "
    "'smash that like', 'don't scroll', 'you owe me a like'. Meta demotes it. Instead ask a REAL question people want "
    "to answer, or make a hot take they'll argue with. 2) No violent or death words even for games: say 'took out', "
    "'unalived', 'cooked', 'destroyed', 'sent him to the lobby' instead of kill/murder/die/dead/shoot/blood. "
    "Never put sexual words or items (sex toys, body parts, porn) in titles, captions, hashtags or on-screen text, even "
    "if they were said in the clip; build the title around something else that happens. "
    "3) No strong swear words in titles (bleep them like f*ck). 4) No drugs, alcohol, sex, politics, crypto or "
    "giveaways. 5) No rage bait.")
