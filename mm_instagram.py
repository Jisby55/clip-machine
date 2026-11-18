"""
Instagram poster (Instagram API with Instagram Login, Professional account).

One-time setup (python machine.py setup-instagram walks through it):
  1. developers.facebook.com -> your app -> add the Instagram use case ("Manage messaging & content on Instagram")
  2. API setup with Instagram login -> add your Instagram account (as an Instagram Tester, accept the invite
     in Instagram: Settings -> Apps and websites -> Tester invites) -> Generate token
  3. put INSTAGRAM_TOKEN=the-token in secrets.env   (never paste it in chat)
The token lasts 60 days; this module refreshes it automatically and keeps it in instagram_token.json.

Instagram fetches images from a public link, so while posting this module opens a temporary private
link to ONE image folder on this PC (Cloudflare quick tunnel, random URL, closed right after).
Reels upload straight from the file (no link needed).
"""
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

import requests

IG_API = "https://graph.instagram.com/v25.0"
FB_API = "https://graph.facebook.com/v25.0"  # tokens from Meta's "Unified login" (EAA...) use this host
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v25.0"
TOKEN_FILE = "instagram_token.json"
PORT = 8724


# ---------- token ----------
def _load():
    t = os.environ.get("INSTAGRAM_TOKEN")
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            saved = json.load(f)
        # a NEW token in secrets.env wins over the saved one (only the bot's own refreshes are kept)
        if not t or saved.get("from_env") == t:
            return saved
        return {"access_token": t, "refreshed_at": 0, "from_env": t}
    if t:
        return {"access_token": t, "refreshed_at": 0, "from_env": t}
    if not t:
        raise SystemExit("Instagram isn't connected yet. Run: python machine.py setup-instagram")
    return {"access_token": t, "refreshed_at": 0}


def _save(tok):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f)


def token():
    tok = _load()
    if time.time() - tok.get("refreshed_at", 0) > 7 * 86400:  # refresh weekly (valid 60 days)
        if tok["access_token"].startswith("EAA"):  # Facebook-style tokens are refreshed by Meta, not here
            _save(dict(tok, refreshed_at=time.time()))
            return tok["access_token"]
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": tok["access_token"]}, timeout=30).json()
        if "access_token" in r:
            tok = dict(tok, access_token=r["access_token"], refreshed_at=time.time())
            _save(tok)
        elif tok.get("refreshed_at", 0) == 0:
            _save(dict(tok, refreshed_at=time.time()))  # brand-new token (<24h old can't refresh yet)
    return tok["access_token"]


def _api():
    tok = _load()
    if tok.get("host"):  # remembered from setup
        return tok["host"]
    return FB_API if token().startswith("EAA") else IG_API


def _get(path, **params):
    r = requests.get(f"{_api()}/{path}", params=dict(params, access_token=token()), timeout=60).json()
    if "error" in r:
        raise RuntimeError(f"Instagram API error: {r['error'].get('message')}")
    return r


def _post(path, **data):
    r = requests.post(f"{_api()}/{path}", data=dict(data, access_token=token()), timeout=120).json()
    if "error" in r:
        raise RuntimeError(f"Instagram API error: {r['error'].get('message')} ({r['error'].get('error_user_msg', '')})")
    return r


def _raw_get(host, path, **params):
    return requests.get(f"{host}/{path}", params=dict(params, access_token=token()), timeout=60).json()


def whoami():
    """Find the Instagram account this token can post for. Meta hands out a few token styles, so try each
    way in turn, remember the one that works (host + IG id) and reuse it."""
    tok = _load()
    if tok.get("ig_id") and tok.get("host"):
        return tok["ig_id"], tok.get("ig_username"), tok.get("ig_kind")
    tries = []
    found = None
    page_token = None
    # A) Facebook user token -> the Facebook Page linked to the Instagram account. Preferred: the Page's
    #    own token never expires when it comes from a long-lived (60-day "extended") user token.
    r = _raw_get(FB_API, "me/accounts", fields="name,access_token,instagram_business_account{id,username}")
    for page in r.get("data", []):
        ig = page.get("instagram_business_account")
        if ig:
            found = (FB_API, ig["id"], ig.get("username"), "BUSINESS")
            page_token = page.get("access_token")
            break
    else:
        tries.append(f"facebook pages: {r.get('error', {}).get('message') or str(len(r.get('data', []))) + ' pages, none with Instagram'}")
    # 0) Known account id from machine_config.json ("instagram_id"): ask each host about it directly
    try:
        import mm_safety
        known = str(mm_safety.config().get("instagram_id") or "")
    except Exception:
        known = ""
    if known and not found:
        for host in (FB_API, IG_API):
            r = _raw_get(host, known, fields="id,username")
            if "error" not in r and r.get("id"):
                found = (host, r["id"], r.get("username"), "BUSINESS")
                break
            tries.append(f"id {known} on {host.split('/')[2]}: {r.get('error', {}).get('message')}")
    # 1) Instagram-login token (graph.instagram.com/me)
    if not found:
        r = _raw_get(IG_API, "me", fields="user_id,username,account_type")
        if "error" not in r and (r.get("user_id") or r.get("id")):
            found = (IG_API, r.get("user_id") or r.get("id"), r.get("username"), r.get("account_type"))
        else:
            tries.append(f"instagram host: {r.get('error', {}).get('message')}")
    # 2) Token whose 'me' IS the Instagram account on graph.facebook.com (Unified login, IG user)
    if not found:
        r = _raw_get(FB_API, "me", fields="id,name")
        if "error" not in r and r.get("id", "").startswith("1784"):  # Instagram ids start with 1784
            found = (FB_API, r["id"], r.get("name"), "BUSINESS")
        else:
            tries.append(f"facebook host me: {r.get('error', {}).get('message') or 'no username'}")
    # 4) Business portfolio -> owned Instagram accounts
    if not found:
        r = _raw_get(FB_API, "me/businesses", fields="instagram_accounts{id,username}")
        for biz in r.get("data", []):
            for ig in biz.get("instagram_accounts", {}).get("data", []):
                found = (FB_API, ig["id"], ig.get("username"), "BUSINESS")
                break
            if found:
                break
        else:
            tries.append(f"business: {r.get('error', {}).get('message') or 'none'}")
    if not found:
        raise RuntimeError("Couldn't find the Instagram account with this token. Tried -> " + " | ".join(tries))
    host, ig_id, name, kind = found
    tok.update({"host": host, "ig_id": ig_id, "ig_username": name, "ig_kind": kind,
                "refreshed_at": tok.get("refreshed_at") or time.time()})
    if page_token:
        tok["access_token"] = page_token  # posts go out as the Page-linked Instagram account
        tok["page_token"] = True
    _save(tok)
    return ig_id, name, kind


def _uid():
    return whoami()[0]


# ---------- temporary public link for images ----------
def _cloudflared():
    exe = shutil.which("cloudflared")
    for p in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"):
        if not exe and os.path.exists(p):
            exe = p
    return exe


@contextmanager
def public_folder(paths):
    """Serve copies of `paths` from a temp folder under random names via a quick tunnel. Yields their URLs."""
    exe = _cloudflared()
    if not exe:
        raise RuntimeError("cloudflared isn't installed (Windows: reopen windows\\clip-bot.bat, it installs it. Mac: brew install cloudflared)")
    d = tempfile.mkdtemp(prefix="clip_pub_")
    names = []
    for p in paths:
        n = f"{uuid.uuid4().hex}.jpg"
        shutil.copy(p, os.path.join(d, n))
        names.append(n)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=d, **k)

        def list_directory(self, path):  # no folder listing, files only
            self.send_error(404)

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        import queue
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
        base, t0 = None, time.time()
        while time.time() - t0 < 60 and not base:
            try:
                line = lines.get(timeout=2)
            except queue.Empty:
                continue
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line or "")
            if m:
                base = m.group(0)
        if not base:
            raise RuntimeError("couldn't open the temporary link (cloudflared)")
        urls = [f"{base}/{n}" for n in names]
        for _ in range(30):  # wait until the link actually works from the internet
            try:
                if requests.head(urls[0], timeout=5).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)
        yield urls
    finally:
        proc.terminate()
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(d, ignore_errors=True)


# ---------- publishing ----------
def _wait_ready(container, limit=300):
    for _ in range(limit // 5):
        st = _get(container, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the media ({st})")
        time.sleep(5)
    raise RuntimeError("Instagram processing timed out")


def _publish(uid, container):
    _wait_ready(container)
    return _post(f"{uid}/media_publish", creation_id=container)["id"]


def post_images(paths, caption):
    """1 image -> photo post; 2-10 -> carousel. Returns the media id."""
    uid = _uid()
    with public_folder(paths) as urls:
        if len(urls) == 1:
            c = _post(f"{uid}/media", image_url=urls[0], caption=caption)["id"]
        else:
            kids = [_post(f"{uid}/media", image_url=u, is_carousel_item="true")["id"] for u in urls[:10]]
            for k in kids:
                _wait_ready(k)
            c = _post(f"{uid}/media", media_type="CAROUSEL", children=",".join(kids), caption=caption)["id"]
        return _publish(uid, c)  # publish while the link is still open


def _upload_reel(uid, path, caption):
    """New Reel container + resumable upload of the local file. Returns the container id once Instagram processed it."""
    c = _post(f"{uid}/media", media_type="REELS", upload_type="resumable", caption=caption, share_to_feed="true")
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        r = requests.post(f"{RUPLOAD}/{c['id']}", data=f.read(), timeout=600, headers={
            "Authorization": f"OAuth {token()}", "offset": "0", "file_size": str(size)})
    try:
        ok = r.json().get("success", True)
    except ValueError:
        ok = False
    if r.status_code >= 300 or not ok:
        raise RuntimeError(f"Reel upload failed {r.status_code}: {r.text[:300]}")
    _wait_ready(c["id"])
    return c["id"]


def _ig_safe_copy(path):
    """Re-encode to Instagram's recommended Reel format (H.264 ~3.5 Mbps, AAC 48 kHz, faststart)."""
    out = os.path.join(tempfile.gettempdir(), f"ig_{uuid.uuid4().hex}.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-c:v", "libx264", "-preset", "medium",
                    "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p", "-r", "30",
                    "-b:v", "3500k", "-maxrate", "5000k", "-bufsize", "10000k",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k", "-movflags", "+faststart", out],
                   check=True, capture_output=True, timeout=900)
    return out


def post_reel(path, caption):
    """Reel straight from a local file (resumable upload). Instagram's upload processing fails at random now and then
    (ProcessingFailedError, wrongly marked not retriable), so: up to 3 tries with a fresh upload each time, waiting
    in between; the last try uses a re-encoded copy in Instagram's recommended format."""
    uid = _uid()
    last = None
    for attempt in range(3):
        src = path if attempt < 2 else _ig_safe_copy(path)
        try:
            cid = _upload_reel(uid, src, caption)
            return _post(f"{uid}/media_publish", creation_id=cid)["id"]
        except RuntimeError as e:
            last = e
            if attempt < 2:
                wait = (30, 90)[attempt]
                print(f"[instagram] upload try {attempt + 1} failed ({str(e)[:90]}); retrying in {wait}s")
                time.sleep(wait)
        finally:
            if src != path:
                try:
                    os.remove(src)
                except OSError:
                    pass
    raise last


def stats(media_id, reel=False):
    """views/likes/comments/shares/saved for one post (whatever Instagram returns). Reels also get watch time:
    avg_watch (seconds) and skip_rate (% who skipped in the first 3 seconds = how strong the hook is)."""
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saved": 0, "avg_watch": None, "skip_rate": None}
    if reel:
        for metric, key, scale in (("ig_reels_avg_watch_time", "avg_watch", 0.001), ("reels_skip_rate", "skip_rate", 1)):
            try:  # newer metrics: asked one by one so one missing metric can't break the rest
                r = _get(f"{media_id}/insights", metric=metric)
                for m in r.get("data", []):
                    v = m.get("values", [{}])[0].get("value", m.get("total_value", {}).get("value"))
                    if v is not None:
                        out[key] = round(float(v) * scale, 2)
            except Exception:
                pass
    try:
        r = _get(f"{media_id}/insights", metric="views,likes,comments,shares,saved")
        for m in r.get("data", []):
            v = m.get("values", [{}])[0].get("value", m.get("total_value", {}).get("value", 0))
            out[m["name"]] = int(v or 0)
    except Exception:
        r = _get(media_id, fields="like_count,comments_count")
        out["likes"], out["comments"] = r.get("like_count", 0), r.get("comments_count", 0)
    return out


def setup():
    t = os.environ.get("INSTAGRAM_TOKEN")
    if not t and not os.path.exists(TOKEN_FILE):
        raise SystemExit("Put INSTAGRAM_TOKEN in secrets.env first (see README)")
    uid, name, kind = whoami()
    print(f"Instagram connected: @{name} ({kind}), id {uid}")
    if not _cloudflared():
        print("!! cloudflared is missing; picture posts need it. Windows: reopen windows\\clip-bot.bat. Mac: brew install cloudflared.")


# ---------- comments ----------
def comments(media_id):
    """Top-level comments on one of our posts, with who already replied under each."""
    r = _get(f"{media_id}/comments", fields="id,text,username,timestamp,replies{username}", limit=50)
    return r.get("data", [])


def reply(comment_id, text):
    return _post(f"{comment_id}/replies", message=text)["id"]


def post_story(path, is_video=False):
    """Share to your Instagram Story too (free extra reach). Image via the temporary link, video uploaded directly."""
    uid = _uid()
    if is_video:
        c = _post(f"{uid}/media", media_type="STORIES", upload_type="resumable")
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            r = requests.post(f"{RUPLOAD}/{c['id']}", data=f.read(), timeout=600, headers={
                "Authorization": f"OAuth {token()}", "offset": "0", "file_size": str(size)})
        if r.status_code >= 300:
            raise RuntimeError(f"Story upload failed {r.status_code}: {r.text[:200]}")
        return _publish(uid, c["id"])
    with public_folder([path]) as urls:
        c = _post(f"{uid}/media", media_type="STORIES", image_url=urls[0])["id"]
        return _publish(uid, c)
