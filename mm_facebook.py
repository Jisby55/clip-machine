"""
Facebook Page Reels (free). Uses the same never-expiring Page token as Instagram (the your Page).
Meta's Reels Publishing API: start an upload -> send the file -> publish. Limit: 30 API reels per Page per 24h.
Needs the Page token to have pages_manage_posts + pages_read_engagement (python machine.py fb-check tells you).
"""
import json
import os

import requests

import mm_instagram as ig

FB = "https://graph.facebook.com/v25.0"


def _tok():
    return ig.token()


def page():
    """(page_id, page_name). With a Page token, 'me' IS the Page."""
    t = ig._load()
    if t.get("fb_page_id"):
        return t["fb_page_id"], t.get("fb_page_name")
    r = requests.get(f"{FB}/me", params={"fields": "id,name", "access_token": _tok()}, timeout=30).json()
    if "error" in r or not r.get("id"):
        raise RuntimeError(f"Facebook Page not found with this token: {r.get('error', {}).get('message')}")
    t.update(fb_page_id=r["id"], fb_page_name=r.get("name"))
    ig._save(t)
    return r["id"], r.get("name")


def _check(r, what):
    if "error" in r:
        e = r["error"]
        raise RuntimeError(f"Facebook {what} failed: {e.get('message')} {e.get('error_user_msg', '')}".strip())
    return r


def post_reel(path, description, ai_generated=False):
    pid, _ = page()
    start = _check(requests.post(f"{FB}/{pid}/video_reels", timeout=60,
                                 data={"upload_phase": "start", "access_token": _tok()}).json(), "reel start")
    vid = start["video_id"]
    url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/v25.0/{vid}"
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        up = requests.post(url, data=f.read(), timeout=900, headers={
            "Authorization": f"OAuth {_tok()}", "offset": "0", "file_size": str(size)})
    if up.status_code >= 300:
        raise RuntimeError(f"Facebook reel upload failed {up.status_code}: {up.text[:300]}")
    data = {"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "description": description[:2200],
            "access_token": _tok()}
    if ai_generated:
        data["is_ai_generated"] = "true"
    _check(requests.post(f"{FB}/{pid}/video_reels", data=data, timeout=120).json(), "reel publish")
    return vid


def stats(video_id):
    """plays, likes, comments, shares, average watch time (seconds) for one reel."""
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saved": 0, "avg_watch": None}
    r = requests.get(f"{FB}/{video_id}", timeout=30, params={
        "fields": "likes.summary(true).limit(0),comments.summary(true).limit(0)", "access_token": _tok()}).json()
    if "error" not in r:
        out["likes"] = r.get("likes", {}).get("summary", {}).get("total_count", 0)
        out["comments"] = r.get("comments", {}).get("summary", {}).get("total_count", 0)
    r = requests.get(f"{FB}/{video_id}/video_insights", timeout=30, params={"access_token": _tok()}).json()
    for m in r.get("data", []):
        v = (m.get("values") or [{}])[0].get("value")
        if m.get("name") in ("blue_reels_play_count", "fb_reels_total_plays") and isinstance(v, (int, float)):
            out["views"] = max(out["views"], int(v))
        elif m.get("name") == "post_video_avg_time_watched" and isinstance(v, (int, float)):
            out["avg_watch"] = round(v / 1000, 2)  # ms -> s
        elif m.get("name") == "post_video_social_actions" and isinstance(v, dict):
            out["shares"] = int(v.get("share", 0))
    return out


def followers():
    pid, _ = page()
    r = requests.get(f"{FB}/{pid}", params={"fields": "followers_count,fan_count", "access_token": _tok()}, timeout=30).json()
    return int(r.get("followers_count") or r.get("fan_count") or 0)


def check():
    pid, name = page()
    print(f"Facebook Page: {name} ({pid})")
    r = requests.get(f"{FB}/debug_token", params={"input_token": _tok(), "access_token": _tok()}, timeout=30).json()
    scopes = r.get("data", {}).get("scopes", [])
    need = ["pages_manage_posts", "pages_read_engagement"]
    missing = [s for s in need if scopes and s not in scopes]
    print("Permissions:", ", ".join(scopes) or "(couldn't read them)")
    print("Missing for Reels: " + (", ".join(missing) if missing else "nothing, ready to post"))
    try:
        print("Followers:", followers())
    except Exception as e:
        print("Followers unavailable:", e)
