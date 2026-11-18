"""Full Threads login (includes mentions + keyword search, which Meta's "Generate Access Token" button leaves out).
Run:  .venv/bin/python mac/threads-login.py
Saves a 60-day THREADS_TOKEN into secrets.env. Redirect URL https://localhost:8723/threads must be in
Meta app > Use cases > Threads API > Settings > Redirect Callback URLs.
"""
import getpass, http.server, os, secrets, ssl, subprocess, sys, tempfile, threading, urllib.parse, webbrowser
import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import compat
import creator_profile as profile  # noqa: E402  (profile.json: creator name, handles, lore)

APP_ID = profile.get("threads_app_id") or ""
REDIRECT = "https://localhost:8723/threads"
SCOPES = ("threads_basic,threads_content_publish,threads_manage_replies,threads_read_replies,"
          "threads_manage_insights,threads_delete,threads_keyword_search,threads_manage_mentions")

compat.load_env()
secret = os.environ.get("THREADS_APP_SECRET", "").strip()
if not secret:
    print("Meta app > Use cases > Threads API > Settings > Threads app secret > Show, copy it.")
    secret = getpass.getpass("Paste the Threads app secret (hidden), then Return: ").strip()
    compat.set_secret("THREADS_APP_SECRET", secret)

state = secrets.token_urlsafe(16)
got = {}

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] == state and "code" in q:
            got["code"] = q["code"][0]
            msg = "Threads connected. You can close this tab and go back to Claude."
        else:
            got["error"] = q.get("error_description", q.get("error", ["unknown"]))[0]
            msg = "Something went wrong: " + got["error"]
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
        self.wfile.write(msg.encode())
    def log_message(self, *a):
        pass

# Self-signed cert so Threads can redirect to https://localhost
tmp = tempfile.mkdtemp()
cert, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
srv = http.server.HTTPServer(("localhost", 8723), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)

url = "https://threads.net/oauth/authorize?" + urllib.parse.urlencode({
    "client_id": APP_ID, "redirect_uri": REDIRECT, "scope": SCOPES, "response_type": "code", "state": state})
print("\nA Threads page is opening. Click Allow.")
print("Then Chrome warns 'Your connection is not private' (normal, it's your own Mac):")
print("click Advanced > Proceed to localhost.\n")
webbrowser.open(url)
while not got:
    try:
        srv.handle_request()
    except (ssl.SSLError, ConnectionError, OSError):
        continue  # browser rejecting the self-signed cert before the user clicks Proceed
if "error" in got:
    sys.exit("Threads said: " + got["error"])

r = requests.post("https://graph.threads.net/oauth/access_token", data={
    "client_id": APP_ID, "client_secret": secret, "grant_type": "authorization_code",
    "redirect_uri": REDIRECT, "code": got["code"]}, timeout=30).json()
if "access_token" not in r:
    sys.exit("Code exchange failed: " + str(r.get("error", r))[:300])
r2 = requests.get("https://graph.threads.net/access_token", params={
    "grant_type": "th_exchange_token", "client_secret": secret, "access_token": r["access_token"]}, timeout=30).json()
tok = r2.get("access_token") or r["access_token"]
compat.set_secret("THREADS_TOKEN", tok)
days = int(r2.get("expires_in", 0)) // 86400
print(f"Saved new THREADS_TOKEN ({'about %d days' % days if days else 'short-lived'}). Tell Claude it's done.")
