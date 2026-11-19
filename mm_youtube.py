"""
YouTube Shorts uploader + stats for the NEW Shorts channel.

One-time setup (the machine walks you through it with: python machine.py setup-youtube):
  1. Google Cloud project -> enable "YouTube Data API v3" and "YouTube Analytics API"
  2. OAuth consent screen (External, add your Google account as a Test user)
  3. Credentials -> OAuth client ID -> "Desktop app" -> download JSON as  youtube_client_secret.json
  4. Run setup-youtube: a browser opens, pick the NEW Shorts channel, allow. Token saved to youtube_token.json

Until the API project passes Google's audit, uploads are forced PRIVATE by YouTube; the machine
uploads them private. YouTube LOCKS those as private (you can't flip them public; it'd need a re-upload).
After the audit passes set audited=true; with review_mode=true uploads stay private so you can check and publish them.
"""
import os

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly",
          "https://www.googleapis.com/auth/youtube.force-ssl"]  # force-ssl = reply to comments
CLIENT_FILE = "youtube_client_secret.json"
TOKEN_FILE = "youtube_token.json"


def credentials(interactive=False, fresh=False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    # keep whatever scopes the saved login already has (asking for new ones on refresh would break it);
    # new scopes (comment replies) are only requested when you reconnect YouTube
    creds = Credentials.from_authorized_user_file(TOKEN_FILE) if os.path.exists(TOKEN_FILE) and not fresh else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            if "invalid_grant" in str(e):  # access was revoked in the Google account: delete the stored token
                os.remove(TOKEN_FILE)
                creds = None
            else:
                raise
    if (not creds or not creds.valid) and interactive:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not os.path.exists(CLIENT_FILE):
            raise SystemExit(f"Missing {CLIENT_FILE} (put it in {os.path.abspath('.')}). See the setup steps at "
                             "the top of mm_youtube.py.")
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
        print("A browser window is opening: sign in and pick the channel your Shorts should go to.", flush=True)
        creds = flow.run_local_server(port=0, prompt="consent select_account")
    if creds and creds.valid:
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        return creds
    raise SystemExit("YouTube isn't connected yet. Run: python machine.py setup-youtube")


def service(name="youtube", version="v3"):
    from googleapiclient.discovery import build
    return build(name, version, credentials=credentials(), cache_discovery=False)


def whoami():
    r = service().channels().list(part="snippet", mine=True).execute()
    items = r.get("items", [])
    return (items[0]["id"], items[0]["snippet"]["title"]) if items else (None, None)


def upload(path, title, description, tags, public=False, synthetic=False):
    """Upload a Short. synthetic=True sets YouTube's 'altered or synthetic content' disclosure."""
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": [t.lstrip("#") for t in tags][:15], "categoryId": "20"},  # 20 = Gaming
        "status": {"privacyStatus": "public" if public else "private",
                   "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": bool(synthetic)},
    }
    req = service().videos().insert(part="snippet,status", body=body,
                                    media_body=MediaFileUpload(path, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def stats(video_ids):
    """{id: {views, likes, comments, retention}} for uploaded Shorts."""
    out = {}
    if not video_ids:
        return out
    items = []
    for i in range(0, len(video_ids), 50):  # the API takes 50 IDs per call; refresh every stored video
        items += service().videos().list(part="statistics", id=",".join(video_ids[i:i + 50])).execute().get("items", [])
    for it in items:
        s = it.get("statistics", {})
        out[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                         "comments": int(s.get("commentCount", 0)), "shares": 0, "retention": None, "avg_watch": None}
    video_ids = video_ids[-50:]  # retention: newest 50 is plenty for learning
    try:  # average % viewed: the strongest Shorts algorithm signal
        from datetime import date, timedelta
        a = service("youtubeAnalytics", "v2").reports().query(
            ids="channel==MINE", startDate=(date.today() - timedelta(days=60)).isoformat(),
            endDate=date.today().isoformat(), metrics="averageViewPercentage,shares,averageViewDuration",
            dimensions="video", filters="video==" + ",".join(video_ids[:50])).execute()
        for row in a.get("rows", []):
            if row[0] in out:
                out[row[0]]["retention"] = float(row[1])
                out[row[0]]["shares"] = int(row[2])
                out[row[0]]["avg_watch"] = float(row[3])  # seconds
    except Exception as e:
        print(f"[YouTube analytics unavailable: {type(e).__name__}]")
    return out


def comment_threads(video_id):
    """Top-level comments on one of our videos (newest first), with replies."""
    return service().commentThreads().list(part="snippet,replies", videoId=video_id, maxResults=50,
                                           order="time", textFormat="plainText").execute().get("items", [])


def reply_comment(parent_id, text):
    return service().comments().insert(part="snippet", body={"snippet": {"parentId": parent_id,
                                                                         "textOriginal": text}}).execute()["id"]


def has_scope(name):
    import json
    try:
        with open(TOKEN_FILE) as f:
            return any(name in sc for sc in json.load(f).get("scopes", []))
    except Exception:
        return False
