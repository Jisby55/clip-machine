"""
TikTok uploader.

One-time setup (python machine.py setup-tiktok):
  1. developers.tiktok.com -> Manage apps -> create app; add products "Login Kit" and "Content Posting API"
  2. Login Kit -> Desktop -> redirect URI:  http://localhost:8723/callback
  3. Scopes: user.info.basic, video.upload (inbox drafts) and later video.publish (after audit)
  4. Put the Client key/secret in environment variables: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET
  5. setup-tiktok opens the login page; approve; token saved to tiktok_token.json

Until the audit passes: videos go to your TikTok INBOX (you get a notification, add a trending
sound if you want, tap Post). Max 5 pending inbox uploads per 24h (TikTok's rule).
After audit: set "audited": true in machine_config.json -> Direct Post with is_aigc labels.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser

import requests

REDIRECT = "http://localhost:8723/callback"
TOKEN_FILE = "tiktok_token.json"
API = "https://open.tiktokapis.com/v2"


def _keys():
    k, s = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
    if not k or not s:
        raise SystemExit("Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in secrets.env first.")
    return k, s


def login(scopes="user.info.basic,video.upload,user.info.stats,video.list"):
    key, secret = _keys()
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok desktop PKCE uses hex
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": key, "scope": scopes, "response_type": "code", "redirect_uri": REDIRECT,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"TikTok connected. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("localhost", 8723), H)
    webbrowser.open(url)
    print("Approve in the browser window that just opened...")
    while "code" not in got and "error" not in got:
        srv.handle_request()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"TikTok login failed: {got.get('error_description') or got}")
    r = requests.post(f"{API}/oauth/token/", data={
        "client_key": key, "client_secret": secret, "code": got["code"], "grant_type": "authorization_code",
        "redirect_uri": REDIRECT, "code_verifier": verifier}, timeout=30).json()
    _save(r)
    print("TikTok connected:", r.get("scope"))


def _save(tok):
    if "access_token" not in tok:
        raise SystemExit(f"TikTok token error: {tok}")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 86400)) - 300
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f)


def token():
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit("TikTok isn't connected yet. Run: python machine.py setup-tiktok")
    with open(TOKEN_FILE) as f:
        tok = json.load(f)
    if time.time() > tok.get("expires_at", 0):
        key, secret = _keys()
        r = requests.post(f"{API}/oauth/token/", data={
            "client_key": key, "client_secret": secret, "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"]}, timeout=30).json()
        _save(r)
        tok = r
    return tok["access_token"]


def _headers():
    return {"Authorization": f"Bearer {token()}", "Content-Type": "application/json; charset=UTF-8"}


def _upload_file(upload_url, path):
    """PUT the file to the exact upload_url TikTok gave us. urllib is used on purpose: it sends the URL
    byte-for-byte, while `requests` can re-encode characters in the one-time upload token."""
    import urllib.request
    import urllib.error

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(upload_url, data=data, method="PUT", headers={
        "Content-Type": "video/mp4", "Content-Length": str(size),
        "Content-Range": f"bytes 0-{size - 1}/{size}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"TikTok file upload failed {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        host = urllib.parse.urlparse(upload_url).netloc
        raise RuntimeError(f"TikTok file upload failed {e.code} (host {host}, "
                           f"logid {e.headers.get('x-tt-logid')}): {body}")


def upload(path, caption, audited=False, ai_generated=False):
    """Inbox draft (before audit) or Direct Post (after audit). Returns publish_id."""
    size = os.path.getsize(path)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    last = None
    for attempt in range(4):  # a fresh upload link on retry (links are single-use and expire; TikTok flakes with 403)
        if audited:
            info = requests.post(f"{API}/post/publish/creator_info/query/", headers=_headers(), timeout=30).json()
            levels = info.get("data", {}).get("privacy_level_options", [])
            privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in levels else (levels[0] if levels else "SELF_ONLY")
            body = {"post_info": {"title": caption[:2200], "privacy_level": privacy, "is_aigc": bool(ai_generated),
                                  "disable_comment": False, "disable_duet": False, "disable_stitch": False},
                    "source_info": source}
            r = requests.post(f"{API}/post/publish/video/init/", headers=_headers(), json=body, timeout=60).json()
        else:
            r = requests.post(f"{API}/post/publish/inbox/video/init/", headers=_headers(),
                              json={"source_info": source}, timeout=60).json()
        if r.get("error", {}).get("code") not in ("ok", None):
            raise RuntimeError(f"TikTok init failed: {r['error']}")
        try:
            _upload_file(r["data"]["upload_url"], path)
            return r["data"]["publish_id"]
        except RuntimeError as e:
            last = e
            print(f"[tiktok upload attempt {attempt + 1} failed] {e}")
            time.sleep(10 + 20 * attempt)
    raise last


def status(publish_id):
    """Where a post is in TikTok's pipeline (e.g. SEND_TO_USER_INBOX, PUBLISH_COMPLETE, FAILED)."""
    r = requests.post(f"{API}/post/publish/status/fetch/", headers=_headers(),
                      json={"publish_id": publish_id}, timeout=30).json()
    if r.get("error", {}).get("code") not in ("ok", None):
        return "API_ERROR", f"{r['error'].get('code')}: {r['error'].get('message')}"
    return r.get("data", {}).get("status"), r.get("data", {}).get("fail_reason")


def account_stats():
    """Followers/likes/video count for the connected account (needs the user.info.stats scope)."""
    r = requests.get(f"{API}/user/info/", headers=_headers(), timeout=30,
                     params={"fields": "follower_count,following_count,likes_count,video_count,display_name"}).json()
    if r.get("error", {}).get("code") not in ("ok", None):
        raise RuntimeError(f"TikTok stats: {r['error'].get('code')} {r['error'].get('message')}")
    return r.get("data", {}).get("user", {})


def recent_videos(n=20, since=None):
    """Your latest public TikToks with views/likes/comments/shares (needs the video.list scope).
    Pages through (20 per page) until it has n videos or reaches videos older than `since` (unix time)."""
    out, cursor = [], None
    while len(out) < n:
        body = {"max_count": 20}
        if cursor:
            body["cursor"] = cursor
        r = requests.post(f"{API}/video/list/", headers=_headers(), timeout=30, json=body,
                          params={"fields": "id,title,video_description,create_time,view_count,like_count,comment_count,"
                                            "share_count"}).json()
        if r.get("error", {}).get("code") not in ("ok", None):
            raise RuntimeError(f"TikTok videos: {r['error'].get('code')} {r['error'].get('message')}")
        d = r.get("data", {})
        page = d.get("videos", [])
        out += page
        if not d.get("has_more") or not page or (since and min(v.get("create_time", 0) for v in page) < since):
            break
        cursor = d.get("cursor")
    return out[:n]
