"""
Extra screens for the Clip Machine app: Queue (what's about to be posted), History (every post everywhere and how
it did vs usual), People (everyone the bot remembers), the Settings editor (no more JSON) and the Series editor.
"""
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

DB = "threadsbot.db"
CFG = "machine_config.json"
BG, PANEL, FG, MUTED, ACCENT, GOOD, BAD = "#14111c", "#1f1a2b", "#ece8f6", "#a59fb8", "#8b45ff", "#3ecf8e", "#ff5c7a"
LINE, WARN = "#2c2540", "#ffb454"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import compat  # noqa: E402  (Windows/Mac helpers)
SCALE = [1.0]  # set by the app on high-DPI screens so sizes given in pixels stay the right size


def px(n):
    return int(round(n * SCALE[0]))


def set_text(box, text, keep="top"):
    """Fill a read-only text box without the view jumping (keep='end' follows the bottom, like a log)."""
    if getattr(box, "_shown", None) == text:
        return
    at, at_end = box.yview()[0], box.yview()[1] > 0.98
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("end", text)
    box.configure(state="disabled")
    box._shown = text
    if keep == "end" and at_end:
        box.see("end")
    else:
        box.yview_moveto(at)


def _db():
    c = sqlite3.connect(DB, timeout=10)
    return c


def _q(sql, args=()):
    try:
        c = _db()
        rows = c.execute(sql, args).fetchall()
        c.close()
        return rows
    except Exception:
        return []


def style(root):
    s = ttk.Style(root)
    s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=px(26), borderwidth=0,
                bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
    s.configure("Treeview.Heading", background="#2c2540", foreground=FG, relief="flat")
    s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])
    s.configure("Small.TButton", background=LINE, foreground=FG, padding=(10, 6), borderwidth=0)
    s.map("Small.TButton", background=[("active", "#3a3154"), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
    s.configure("Go.TButton", background=ACCENT, foreground="white", padding=(14, 8), borderwidth=0,
                font=("Segoe UI Semibold", 10))
    s.map("Go.TButton", background=[("active", "#a46bff"), ("disabled", PANEL)], foreground=[("disabled", MUTED)])


def _table(parent, cols, widths):
    f = tk.Frame(parent, bg=PANEL)
    f.pack(fill="both", expand=True, padx=6, pady=6)
    tv = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
    for c, w in zip(cols, widths):
        tv.heading(c, text=c, command=lambda c=c: _sort(tv, c, False))
        tv.column(c, width=w, anchor="w", stretch=c == cols[-1] or w > 250)
    sb = ttk.Scrollbar(f, command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(fill="both", expand=True)
    tv.tag_configure("good", foreground=GOOD)
    tv.tag_configure("bad", foreground=BAD)
    tv.tag_configure("muted", foreground=MUTED)
    return tv


def _sort(tv, col, rev):
    rows = [(tv.set(k, col), k) for k in tv.get_children("")]
    def key(x):
        try:  # numbers first (sorted as numbers), then text; mixing them used to crash
            return (0, float(x[0].replace("+", "").replace("%", "").replace(",", "")), "")
        except ValueError:
            return (1, 0.0, x[0].lower())
    rows.sort(key=key, reverse=rev)
    for i, (_, k) in enumerate(rows):
        tv.move(k, "", i)
    tv.heading(col, command=lambda: _sort(tv, col, not rev))


def _bar(parent):
    b = tk.Frame(parent, bg=PANEL)
    b.pack(fill="x", padx=6, pady=(6, 0))
    return b


def _verdict(r):
    if r is None:
        return "", "muted"
    return (f"{r:+.2f} " + ("better" if r > 0.15 else "worse" if r < -0.15 else "usual")), \
        ("good" if r > 0.15 else "bad" if r < -0.15 else "")


# ======================= TikTok =======================
TIKTOK_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload"


def _ffmpeg():
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _old_captions():
    """Captions written before they were saved with each video (tiktok_captions.txt): [(local datetime, text)]."""
    from datetime import datetime
    out = []
    try:
        with open("tiktok_captions.txt", encoding="utf-8") as f:
            for line in f:
                if line.startswith("[") and "] " in line:
                    when, text = line[1:].split("] ", 1)
                    try:
                        out.append((datetime.strptime(when, "%Y-%m-%d %H:%M"), text.split("  [When posting:")[0].strip()))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


class TikTokTab:
    """Every TikTok video the bot got ready: watch it, copy its caption, post it yourself, tick it off."""
    W, H = 243, 432

    def __init__(self, app, nb):
        self.app = app
        self.rows = {}
        self.cur = None
        self.thumb = None
        self._play = None  # (process, token) while the in-app preview is running
        self._token = 0
        for col in ("caption TEXT", "done INTEGER DEFAULT 0"):
            try:
                c = _db()
                c.execute(f"ALTER TABLE mm_posts ADD COLUMN {col}")
                c.commit()
                c.close()
            except Exception:
                pass
        f = tk.Frame(nb, bg=PANEL)
        nb.add(f, text="TikTok")
        self.frame = f

        top = _bar(f)
        self.head = tk.Label(top, text="", bg=PANEL, fg=FG, font=("Segoe UI Semibold", 11))
        self.head.pack(side="left")
        ttk.Button(top, text="↻ Refresh", style="Small.TButton", command=self.fill).pack(side="right")
        ttk.Button(top, text="➕ Get a new video ready now", style="Small.TButton", command=self.make_one).pack(side="right", padx=6)
        self.auto = tk.BooleanVar(value=self._cfg().get("platforms", {}).get("tiktok", {}).get("auto_inbox", False))
        tk.Checkbutton(top, text="Auto-send new videos to my phone's TikTok inbox", variable=self.auto,
                       command=self._save_auto, bg=PANEL, fg=MUTED, activebackground=PANEL, activeforeground=FG,
                       selectcolor=BG, highlightthickness=0).pack(side="right", padx=6)

        body = tk.Frame(f, bg=PANEL)
        body.pack(fill="both", expand=True, padx=6, pady=6)
        left = tk.Frame(body, bg=PANEL, width=px(370))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.tv = _table(left, ("status", "made", "title"), (px(118), px(92), px(140)))
        self.tv.tag_configure("wait", foreground=WARN)
        self.tv.bind("<<TreeviewSelect>>", lambda e: self.show())
        self.tv.bind("<Double-1>", lambda e: self.play())

        right = tk.Frame(body, bg=PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(10, 4))
        vid = tk.Frame(right, bg=PANEL)
        vid.pack(side="left", anchor="n", pady=6)
        self.screen = tk.Label(vid, bg="#000000", fg=MUTED, text="pick a video", width=px(self.W), height=px(self.H),
                               cursor="hand2", compound="center")
        self.blank = tk.PhotoImage(width=px(self.W), height=px(self.H))
        self.screen.configure(image=self.blank)
        self.screen.pack()
        self.screen.bind("<Button-1>", lambda e: self.preview())
        row = tk.Frame(vid, bg=PANEL)
        row.pack(fill="x", pady=(6, 0))
        self.b_prev = ttk.Button(row, text="▶ Watch here", style="Small.TButton", command=self.preview)
        self.b_prev.pack(side="left", fill="x", expand=True, padx=(0, 3))
        ttk.Button(row, text="🔊 With sound", style="Small.TButton", command=self.play).pack(side="left", fill="x", expand=True, padx=(3, 0))
        tk.Label(vid, text="Watch here = quick look, no sound.", bg=PANEL, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(3, 0))

        info = tk.Frame(right, bg=PANEL)
        info.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=6)
        self.title = tk.Label(info, text="", bg=PANEL, fg=FG, font=("Segoe UI Semibold", 13), anchor="w", justify="left")
        self.title.pack(fill="x")
        self.state = tk.Label(info, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left")
        self.state.pack(fill="x", pady=(2, 8))
        tk.Label(info, text="CAPTION (edit it if you want)", bg=PANEL, fg=MUTED,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w")
        self.cap = tk.Text(info, bg=BG, fg=FG, insertbackground=FG, relief="flat", wrap="word", height=7,
                           font=("Segoe UI", 11), padx=10, pady=8, undo=True, highlightthickness=0)
        self.cap.pack(fill="both", expand=True, pady=(3, 6))
        self.count = tk.Label(info, text="", bg=PANEL, fg=MUTED, anchor="e", font=("Segoe UI", 8))
        self.count.pack(fill="x")
        self.cap.bind("<KeyRelease>", lambda e: self._count())

        grid = tk.Frame(info, bg=PANEL)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="b")
        grid.columnconfigure(1, weight=1, uniform="b")
        buttons = [("📋  Copy caption", self.copy, "Go.TButton"), ("🌐  Open TikTok upload", self.open_upload, None),
                   ("📂  Show video file", self.reveal, None), ("📲  Send video to phone", self.send_inbox, None),
                   ("✅  I posted it", lambda: self.mark(1), None), ("⏭  Skip this one", lambda: self.mark(2), None),
                   ("✍  New caption (AI)", self.recaption, None), ("📱  Caption to phone", self.to_phone, None),
                   ("💾  Save my edit", self.save_edit, None), ("↩  Not posted yet", lambda: self.mark(0), None)]
        for i, (text, cmd, st) in enumerate(buttons):
            ttk.Button(grid, text=text, command=cmd, style=st or "Small.TButton").grid(
                row=i // 2, column=i % 2, sticky="ew", padx=(0, 6) if i % 2 == 0 else 0, pady=3)
        self.msg = tk.Label(info, text="", bg=PANEL, fg=GOOD, anchor="w")
        self.msg.pack(fill="x", pady=(2, 0))
        self.help = tk.Label(info, justify="left", anchor="w", bg=PANEL, fg=MUTED, text=(
            "PC: Copy caption → Open TikTok upload → drag the video file in → paste → Post.\n"
            "Phone: Send video to phone → TikTok inbox → paste the caption → Post.\n"
            "Then press I posted it. The bot stops making new ones while 4 are waiting."))
        self.help.pack(fill="x", pady=(6, 0))
        info.bind("<Configure>", lambda e: [w.configure(wraplength=max(200, e.width - 8))
                                            for w in (self.title, self.state, self.help)])
        self.fill()

    # ---------- data ----------
    def _cfg(self):
        try:
            with open(CFG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_auto(self):
        cfg = self._cfg()
        if not cfg:
            return
        cfg.setdefault("platforms", {}).setdefault("tiktok", {})["auto_inbox"] = bool(self.auto.get())
        with open(CFG + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(CFG + ".tmp", CFG)

    def waiting(self):
        return sum(1 for r in self.rows.values() if r["state"] in ("ready", "inbox"))

    def fill(self):
        from datetime import datetime, timedelta
        old = _old_captions()
        keep = self.cur
        self.rows = {}
        self.tv.delete(*self.tv.get_children())
        for pid, clip_id, at, privacy, title, caption, media, done, views, likes, path, ai in _q(
                "SELECT p.id, p.clip_id, p.posted_at, p.privacy, COALESCE(p.title,''), p.caption, COALESCE(p.media,''), "
                "COALESCE(p.done,0), p.views, p.likes, cl.path, COALESCE(cl.ai_visual,0)+COALESCE(cl.ai_voice,0) "
                "FROM mm_posts p LEFT JOIN mm_clips cl ON cl.id=p.clip_id WHERE p.platform='tiktok' "
                "ORDER BY p.id DESC LIMIT 80"):
            try:
                local = datetime.fromisoformat(at).astimezone().replace(tzinfo=None)
            except Exception:
                local = None
            if not caption and local:  # older videos: the caption is in tiktok_captions.txt
                near = [t for w, t in old if abs((w - local).total_seconds()) < 900]
                caption = near[0] if near else None
            if "tiktok_video_id" in media or done == 1 or privacy == "public":
                state = "posted"
            elif done == 2:
                state = "skipped"
            else:
                state = "inbox" if privacy == "inbox" else "ready"
            label = {"posted": "✅ posted" + (f" · {views} views" if views else ""), "skipped": "skipped",
                     "inbox": "📲 in your inbox", "ready": "● ready to post"}[state]
            self.rows[str(pid)] = {"id": pid, "clip": clip_id, "title": title, "caption": caption or title, "state": state,
                                   "path": (path or "").replace("\\", os.sep), "ai": bool(ai), "views": views, "likes": likes,
                                   "when": local, "found": "tiktok_video_id" in media}
            self.tv.insert("", "end", iid=str(pid), tags=("wait" if state in ("ready", "inbox") else "muted",),
                           values=(label, local.strftime("%b %d %H:%M") if local else "", title))
        n = self.waiting()
        self.head.configure(text=(f"{n} video{'s' if n != 1 else ''} waiting for you to post" if n else
                                  "Nothing waiting. The bot gets the next one ready on its schedule."),
                            fg=WARN if n else MUTED)
        try:
            self.app.tabs.tab(self.frame, text=f"TikTok ({n})" if n else "TikTok")
        except Exception:
            pass
        pick = keep if keep in self.rows else next((k for k, r in self.rows.items() if r["state"] in ("ready", "inbox")),
                                                   next(iter(self.rows), None))
        if pick:
            self.tv.selection_set(pick)
            self.tv.see(pick)
        else:
            self.show()

    def _row(self):
        s = self.tv.selection()
        return self.rows.get(s[0]) if s else None

    def _count(self):
        n = len(self.cap.get("1.0", "end-1c"))
        self.count.configure(text=f"{n} / 2200 characters", fg=BAD if n > 2200 else MUTED)

    def show(self):
        r = self._row()
        if r and str(r["id"]) == self.cur and self.cap.edit_modified():
            return  # same video and you're typing: don't wipe your edit
        self.stop_preview()
        self.cur = str(r["id"]) if r else None
        self.msg.configure(text="")
        self.cap.delete("1.0", "end")
        if not r:
            self.title.configure(text="No TikTok videos yet")
            self.state.configure(text="The bot puts each TikTok video here with its caption when it's ready.")
            self.screen.configure(image=self.blank, text="")
            return
        self.cap.insert("end", r["caption"])
        self.cap.edit_modified(False)
        self._count()
        self.title.configure(text=r["title"] or "(no title)")
        bits = [{"posted": "Posted" + (f": {r['views'] or 0} views, {r['likes'] or 0} likes" if r["found"] else ""),
                 "skipped": "Skipped (won't be posted)", "inbox": "Waiting in your TikTok inbox on your phone",
                 "ready": "Ready: post it from your PC, or send it to your phone"}[r["state"]]]
        if r["when"]:
            bits.append("made " + r["when"].strftime("%a %b %d, %I:%M %p").replace(" 0", " "))
        if not (r["path"] and os.path.exists(r["path"])):
            bits.append("the video file isn't on this PC anymore")
        self.state.configure(text="  ·  ".join(bits) + ("\n⚠ AI-made: turn ON 'AI-generated content' when you post it."
                                                        if r["ai"] else ""), fg=WARN if r["ai"] else MUTED)
        self.screen.configure(image=self.blank, text="loading...")
        threading.Thread(target=self._load_thumb, args=(r, self.cur), daemon=True).start()

    def _load_thumb(self, r, token):
        png = os.path.join("media", "thumbs", f"clip_{r['clip']}_{px(self.W)}.png")
        if not os.path.exists(png) and r["path"] and os.path.exists(r["path"]):
            ff = _ffmpeg()
            if ff:
                os.makedirs(os.path.dirname(png), exist_ok=True)
                w, h = px(self.W), px(self.H)
                subprocess.run([ff, "-y", "-ss", "1", "-i", r["path"], "-frames:v", "1", "-vf",
                                f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", png],
                               capture_output=True, creationflags=NOWIN)

        def apply():
            if self.cur != token or self._play:
                return
            try:
                self.thumb = tk.PhotoImage(file=png)
                self.screen.configure(image=self.thumb, text="")
            except Exception:
                self.screen.configure(image=self.blank, text="(no preview)")
        self.frame.after(0, apply)

    # ---------- in-app preview (frames from ffmpeg, no sound) ----------
    def preview(self):
        if self._play:
            return self.stop_preview(restore=True)
        r = self._row()
        ff = _ffmpeg()
        if not r or not ff or not (r["path"] and os.path.exists(r["path"])):
            return self.play()
        w, h = px(self.W), px(self.H)
        try:
            p = subprocess.Popen([ff, "-loglevel", "quiet", "-re", "-i", r["path"], "-an", "-vf",
                                  f"fps=15,scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                                  "-f", "image2pipe", "-vcodec", "ppm", "-"], stdout=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, creationflags=NOWIN)
        except Exception:
            return self.play()
        self._token += 1
        token = self._token
        self._play = (p, token)
        self.b_prev.configure(text="⏹ Stop")
        frames = queue.Queue(maxsize=8)
        head = f"P6\n{w} {h}\n255\n".encode()
        size = len(head) + w * h * 3

        def reader():
            try:
                while True:
                    buf = p.stdout.read(size)
                    if len(buf) < size:
                        break
                    frames.put(buf)
            except Exception:
                pass
            frames.put(None)
        threading.Thread(target=reader, daemon=True).start()

        def tick():
            if not self._play or self._play[1] != token:
                return
            try:
                buf = frames.get_nowait()
            except queue.Empty:
                return self.frame.after(20, tick)
            if buf is None:
                return self.stop_preview(restore=True)
            try:
                self.thumb = tk.PhotoImage(data=buf, format="ppm")
                self.screen.configure(image=self.thumb, text="")
            except Exception:
                self.stop_preview(restore=True)
                return self.play()
            self.frame.after(45, tick)
        tick()

    def stop_preview(self, restore=False):
        if self._play:
            try:
                self._play[0].kill()
            except Exception:
                pass
            self._play = None
            self.b_prev.configure(text="▶ Watch here")
            r = self._row()
            if restore and r:
                threading.Thread(target=self._load_thumb, args=(r, self.cur), daemon=True).start()

    # ---------- buttons ----------
    def _say(self, text, ok=True):
        self.msg.configure(text=text, fg=GOOD if ok else BAD)

    def _need(self, file=False):
        r = self._row()
        if not r:
            self._say("Pick a video on the left first.", False)
            return None
        if file and not (r["path"] and os.path.exists(r["path"])):
            self._say("That video file isn't on this PC anymore.", False)
            return None
        return r

    def play(self):
        r = self._need(file=True)
        if r:
            self.stop_preview(restore=True)
            compat.open_file(os.path.abspath(r["path"]))

    def copy(self):
        if self._need():
            text = self.cap.get("1.0", "end-1c").strip()
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
            self._say("Caption copied. Paste it into TikTok.")

    def reveal(self):
        r = self._need(file=True)
        if r:
            subprocess.Popen(["explorer", "/select,", os.path.abspath(r["path"])])

    def open_upload(self):
        import webbrowser
        webbrowser.open(TIKTOK_UPLOAD)
        self.copy()

    def _saved(self, r):
        """Keep an edited caption before anything that reads it from the database."""
        text = self.cap.get("1.0", "end-1c").strip()
        if text and text != r["caption"]:
            c = _db()
            c.execute("UPDATE mm_posts SET caption=? WHERE id=?", (text, r["id"]))
            c.commit()
            c.close()
            r["caption"] = text
        self.cap.edit_modified(False)

    def save_edit(self):
        r = self._need()
        if r:
            self._saved(r)
            self._say("Saved your caption.")

    def mark(self, done):
        r = self._need()
        if r:
            c = _db()
            c.execute("UPDATE mm_posts SET done=? WHERE id=?", (done, r["id"]))
            c.commit()
            c.close()
            self.cap.edit_modified(False)
            self.fill()
            self._say({1: "Marked as posted.", 2: "Skipped.", 0: "Back in the waiting list."}[done])

    def send_inbox(self):
        r = self._need(file=True)
        if r and messagebox.askyesno("TikTok inbox", "Send this video to your phone's TikTok inbox now?\n\n"
                                     "(TikTok refuses new ones while too many old drafts are still unposted there.)"):
            self._saved(r)
            self.app.run(["machine.py", "tiktok-send", str(r["id"])], live=True, title="Send the video to your TikTok inbox",
                         then=self.fill)

    def to_phone(self):
        r = self._need()
        if r:
            self._saved(r)
            self.app.run(["machine.py", "tiktok-phone", str(r["id"])], live=True, title="Send the caption to your phone")

    def recaption(self):
        r = self._need()
        if r:
            self.cap.edit_modified(False)
            self.cur = None
            self.app.run(["machine.py", "tiktok-recaption", str(r["id"])], live=True, title="Write a new TikTok caption",
                         then=lambda: (self.fill(), self.app.tabs.select(self.frame)))

    def make_one(self):
        if messagebox.askyesno("TikTok", "Get the next TikTok video + caption ready now?"):
            self.app.run(["machine.py", "force", "tiktok"], live=True, title="Get a TikTok video ready",
                         then=lambda: (self.fill(), self.app.tabs.select(self.frame)))



# ======================= Videos (what the bot is allowed to clip) =======================
class VideosTab:
    """Every long video and stream on your channel. Star the ones the bot should clip next, block the ones it should
    never touch, or make it use ONLY your starred videos."""

    def __init__(self, app, nb):
        self.app = app
        try:
            c = _db()
            c.execute("""CREATE TABLE IF NOT EXISTS mm_library (video_id TEXT PRIMARY KEY, title TEXT, duration REAL,
                         views INTEGER, is_stream INTEGER, indexed_at TEXT)""")
            try:
                c.execute("ALTER TABLE mm_library ADD COLUMN priority INTEGER DEFAULT 0")
            except Exception:
                pass
            c.commit()
            c.close()
        except Exception:
            pass
        f = tk.Frame(nb, bg=PANEL)
        nb.add(f, text="Videos")
        self.frame = f
        top = _bar(f)
        for text, cmd in (("⭐ Use next", lambda: self.mark(2)), ("🚫 Never use", lambda: self.mark(-1)),
                          ("↩ Back to normal", lambda: self.mark(0)), ("✂ Make a clip from it now", self.clip_now),
                          ("▶ Open on YouTube", self.open_yt)):
            ttk.Button(top, text=text, style="Small.TButton", command=cmd).pack(side="left", padx=(0, 6))
        bar2 = _bar(f)
        ttk.Button(bar2, text="⬇ Download + find moments in starred", style="Go.TButton", command=self.fetch).pack(side="left", padx=(0, 6))
        ttk.Button(bar2, text="↻ Re-read my channel", style="Small.TButton", command=self.reindex).pack(side="left", padx=(0, 6))
        self.only = tk.BooleanVar(value=self._cfg().get("source_mode", "random") == "picked")
        tk.Checkbutton(bar2, text="Only use videos I starred (plus brand-new uploads)", variable=self.only,
                       command=self._save_mode, bg=PANEL, fg=FG, activebackground=PANEL, activeforeground=FG,
                       selectcolor=BG, highlightthickness=0).pack(side="left", padx=8)
        bar3 = _bar(f)
        tk.Label(bar3, text="Search:", bg=PANEL, fg=MUTED).pack(side="left")
        self.search = tk.StringVar()
        e = tk.Entry(bar3, textvariable=self.search, bg=LINE, fg=FG, insertbackground=FG, relief="flat", width=28)
        e.pack(side="left", padx=6, ipady=3)
        e.bind("<KeyRelease>", lambda ev: self.fill())
        self.show = tk.StringVar(value="all")
        for name in ("all", "videos", "streams", "starred", "has moments", "blocked"):
            tk.Radiobutton(bar3, text=name, value=name, variable=self.show, command=self.fill, bg=PANEL, fg=FG,
                           selectcolor=BG, activebackground=PANEL, activeforeground=FG, highlightthickness=0).pack(side="left")
        self.info = tk.Label(bar3, text="", bg=PANEL, fg=MUTED)
        self.info.pack(side="right")
        self.tv = _table(f, ("pick", "title", "type", "length", "views", "status", "clips"),
                         (px(60), px(430), px(70), px(70), px(80), px(230), px(60)))
        self.tv.configure(selectmode="extended")
        self.tv.tag_configure("star", foreground=WARN)
        self.tv.tag_configure("ready", foreground=GOOD)
        self.tv.bind("<Double-1>", lambda ev: self.open_yt())
        tk.Label(f, bg=PANEL, fg=MUTED, anchor="w", justify="left", text=(
            "Select one or more (Ctrl/Shift-click). ⭐ = clipped before anything else.  🚫 = never used.  "
            "Everything else gets picked at random like before.")).pack(fill="x", padx=10, pady=(0, 6))
        self.fill()

    def _cfg(self):
        try:
            with open(CFG, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_mode(self):
        cfg = self._cfg()
        if not cfg:
            return
        cfg["source_mode"] = "picked" if self.only.get() else "random"
        with open(CFG + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(CFG + ".tmp", CFG)

    def fill(self):
        keep = set(self.tv.selection())
        self.tv.delete(*self.tv.get_children())
        want, text = self.show.get(), self.search.get().strip().lower()
        try:
            rows = _q("SELECT l.video_id, COALESCE(l.title,''), COALESCE(l.duration,0), COALESCE(l.views,0), "
                      "COALESCE(l.is_stream,0), COALESCE(l.priority,0), s.video_id IS NOT NULL, COALESCE(s.analyzed,0), "
                      "COALESCE(s.path,''), "
                      "(SELECT COUNT(*) FROM mm_moments m WHERE m.video_id=l.video_id AND m.used=0 AND m.ai_score>=6), "
                      "(SELECT COUNT(*) FROM mm_clips c JOIN mm_moments m ON m.id=c.moment_id WHERE m.video_id=l.video_id) "
                      "FROM mm_library l LEFT JOIN mm_sources s ON s.video_id=l.video_id")
        except Exception:
            rows = []
        rows.sort(key=lambda r: (-(r[5] > 0) * r[5], -(r[9] > 0), -r[3]))
        shown = stars = 0
        for vid, title, dur, views, stream, pri, have, analyzed, path, left, clips in rows:
            stars += pri == 2
            if text and text not in title.lower():
                continue
            if (want == "videos" and stream) or (want == "streams" and not stream) or (want == "starred" and pri < 1) \
                    or (want == "blocked" and pri != -1) or (want == "has moments" and not left):
                continue
            if pri == -1:
                status = "blocked"
            elif left:
                status = f"{left} good moment{'s' if left != 1 else ''} ready"
            elif have and not analyzed:
                status = "downloaded, moments not found yet"
            elif have and not path:
                status = "couldn't be downloaded"
            elif have:
                status = "used up" if clips else "no good moments found"
            else:
                status = "not downloaded yet"
            mins = int(dur // 60)
            self.tv.insert("", "end", iid=vid, tags=("star" if pri >= 1 else "muted" if pri == -1 else "ready" if left else "",),
                           values=({2: "⭐", 1: "new", -1: "🚫"}.get(pri, ""), title, "stream" if stream else "video",
                                   f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins} min",
                                   f"{views:,}", status, clips or ""))
            shown += 1
        self.info.configure(text=f"{shown} of {len(rows)} shown · {stars} starred")
        ok = [k for k in keep if self.tv.exists(k)]
        if ok:
            self.tv.selection_set(ok)

    def mark(self, pri):
        ids = self.tv.selection()
        if not ids:
            return messagebox.showinfo("Videos", "Select one or more videos first.")
        c = _db()
        c.executemany("UPDATE mm_library SET priority=? WHERE video_id=?", [(pri, i) for i in ids])
        c.commit()
        c.close()
        self.fill()

    def open_yt(self):
        import webbrowser
        for i in self.tv.selection()[:3]:
            webbrowser.open(f"https://www.youtube.com/watch?v={i}")

    def fetch(self):
        self.app.run(["machine.py", "fetch-picked"], title="Download + find moments in starred videos (slow for streams)",
                     then=self.fill)

    def reindex(self):
        self.app.run(["machine.py", "index"], title="Re-read my channel's video list", then=self.fill)

    def clip_now(self):
        ids = self.tv.selection()
        if len(ids) != 1:
            return messagebox.showinfo("Videos", "Select exactly one video.")
        self.app.run(["machine.py", "clip-from", ids[0]], title="Make a clip from this video", then=self.fill)


# ======================= Held back (videos the bot didn't post) =======================
def _ensure_clip_cols(c):
    for col in ("approved INTEGER DEFAULT 0", "hold_reason TEXT"):
        try:
            c.execute(f"ALTER TABLE mm_clips ADD COLUMN {col}")
        except Exception:
            pass
    c.commit()


REASONS = {"duplicate": "looked like a moment you already made a Short of",
           "skipped": "its on-screen text/title has words the platforms hold back"}


class HeldTab:
    """Videos the bot made but decided not to post (filters, duplicate check, unfinished runs).
    'It's good!' puts one first in line for the next autopilot cycle and teaches the AI that it was wrong."""

    def __init__(self, app, nb):
        self.app = app
        try:
            c = _db()
            _ensure_clip_cols(c)
            c.close()
        except Exception:
            pass
        f = tk.Frame(nb, bg=PANEL)
        nb.add(f, text="Held back")
        self.frame = f
        bar = _bar(f)
        ttk.Button(bar, text="👍  It's good! Post it next cycle", style="Go.TButton", command=self.approve).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="▶ Watch", style="Small.TButton", command=self.watch).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="📂 Show file", style="Small.TButton", command=self.reveal).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="↻ Refresh", style="Small.TButton", command=self.fill).pack(side="left")
        note = tk.Label(f, bg=PANEL, fg=MUTED, anchor="w", justify="left", text=(
            "Videos the bot made but held back. If it was wrong, press It's good!: the video goes first in line for "
            "the next autopilot cycle, and the bot remembers that this kind of thing is fine."))
        note.pack(fill="x", padx=10)
        f.bind("<Configure>", lambda e: note.configure(wraplength=max(300, e.width - 30)))
        self.tv = _table(f, ("id", "made", "title", "why it was held back"), (px(60), px(110), px(330), px(420)))
        self.tv.bind("<Double-1>", lambda e: self.watch())
        self.msg = tk.Label(f, text="", bg=PANEL, fg=GOOD, anchor="w")
        self.msg.pack(fill="x", padx=10, pady=(0, 6))
        self.rows = {}
        self.fill()

    def fill(self):
        import glob
        self.tv.delete(*self.tv.get_children())
        self.rows = {}
        known = set()
        for cid, made, title, path, status, why in _q(
                "SELECT id, replace(substr(created_at,6,11),'T',' '), COALESCE(title,''), path, status, "
                "COALESCE(hold_reason,'') FROM mm_clips ORDER BY id DESC"):
            known.add(os.path.normcase(os.path.abspath((path or "").replace("\\", os.sep))))
            if status in ("ready", "upload") or why == "you skipped it":
                continue
            self.rows[str(cid)] = {"id": cid, "path": (path or "").replace("\\", os.sep), "title": title,
                                   "why": why or REASONS.get(status, status)}
            self.tv.insert("", "end", iid=str(cid), values=(cid, made, title, self.rows[str(cid)]["why"]))
        # finished videos that never made it into the list (a run that crashed right at the end)
        for p in sorted(glob.glob(os.path.join("media", "clips", "*", "final*.mp4")), reverse=True):
            ap = os.path.normcase(os.path.abspath(p))
            if ap in known:
                continue
            sib = [os.path.normcase(os.path.abspath(os.path.join(os.path.dirname(p), x)))
                   for x in ("final.mp4", "final_voice.mp4")]
            if any(s in known for s in sib):
                continue  # just the version without the AI voice of a clip that's already listed
            key = "f:" + p
            self.rows[key] = {"id": None, "path": p, "title": os.path.basename(os.path.dirname(p)),
                              "why": "finished, but the run stopped before it was added (crash or stopped)"}
            self.tv.insert("", "end", iid=key, values=("-", "", self.rows[key]["title"], self.rows[key]["why"]))
        n = len(self.rows)
        try:
            self.app.tabs.tab(self.frame, text=f"Held back ({n})" if n else "Held back")
        except Exception:
            pass

    def _row(self):
        s = self.tv.selection()
        return self.rows.get(s[0]) if s else None

    def watch(self):
        r = self._row()
        if r and os.path.exists(r["path"]):
            compat.open_file(os.path.abspath(r["path"]))

    def reveal(self):
        r = self._row()
        if r and os.path.exists(r["path"]):
            subprocess.Popen(["explorer", "/select,", os.path.abspath(r["path"])])

    def approve(self):
        r = self._row()
        if not r:
            return self.msg.configure(text="Pick a video first.", fg=BAD)
        c = _db()
        _ensure_clip_cols(c)
        cid = r["id"]
        if cid is None:  # register the lost video
            import re as _re
            folder = os.path.basename(os.path.dirname(r["path"]))
            m = _re.match(r"m(\d+)_", folder)
            mid = int(m.group(1)) if m and c.execute("SELECT 1 FROM mm_moments WHERE id=?", (int(m.group(1)),)).fetchone() \
                else c.execute("INSERT INTO mm_moments (video_id, start, end, text, loudness, ai_score, hook, title, used) "
                               "VALUES ('ai', 0, 0, 'A video you approved', 0, 10, '', ?, 1)", (folder,)).lastrowid
            ai = 1 if folder.startswith("ai_") else 0
            cid = c.execute("INSERT INTO mm_clips (moment_id, path, variant, hook, title, ai_visual, created_at, status) "
                            "VALUES (?,?,?,?,?,?,datetime('now'),'ready')",
                            (mid, r["path"], json.dumps({"type": "ai_video" if ai else "clip"}), "",
                             folder, ai)).lastrowid
        c.execute("UPDATE mm_clips SET status='ready', approved=1 WHERE id=?", (cid,))
        c.commit()
        try:  # the AI learns it was too strict
            import mm_brain
            mm_brain.add_lessons(c, [("all", f"[OWNER] watched a video you held back because it {r['why']} "
                                             f"('{r['title']}') and said it's GOOD. Don't be that strict about this kind "
                                             "of thing.", 7)])
            c.commit()
        except Exception:
            pass
        c.close()
        self.fill()
        self.msg.configure(text=f"Approved. '{r['title']}' goes out first on the next autopilot cycle.", fg=GOOD)


# ======================= Post to... (shared) =======================
def pick_platforms(app, title, kind, then, note=""):
    """Checkboxes for the platforms that are connected + turned on (last pick pre-ticked). then(list) on OK."""
    import mm_targets
    ready = mm_targets.ready(kind)
    if not ready:
        why = "\n".join(f"• {mm_targets.NAMES[p].split(' (')[0]}: {w}" for p, w in mm_targets.missing(kind))
        return messagebox.showinfo(title, "Nothing to post to yet.\n\n" + why, parent=app)
    last = set(mm_targets.last(kind))
    w = tk.Toplevel(app)
    w.title(title)
    w.configure(bg=PANEL)
    w.transient(app)
    tk.Label(w, text="POST TO", bg=PANEL, fg=FG, font=("Segoe UI Semibold", 12)).pack(anchor="w", padx=16, pady=(14, 4))
    if note:
        tk.Label(w, text=note, bg=PANEL, fg=MUTED, wraplength=px(420), justify="left").pack(anchor="w", padx=16)
    vars_ = {}
    for p in ready:
        vars_[p] = tk.BooleanVar(value=p in last)
        tk.Checkbutton(w, text=mm_targets.NAMES[p], variable=vars_[p], bg=PANEL, fg=FG, selectcolor=BG,
                       activebackground=PANEL, activeforeground=FG, highlightthickness=0).pack(anchor="w", padx=16)
    off = mm_targets.missing(kind)
    if off:
        tk.Label(w, bg=PANEL, fg=MUTED, justify="left", wraplength=px(420), text="Not available: " + "; ".join(
            f"{mm_targets.NAMES[p].split(' (')[0]} ({why})" for p, why in off)).pack(anchor="w", padx=16, pady=(8, 0))

    def ok():
        chosen = [p for p, v in vars_.items() if v.get()]
        if not chosen:
            return messagebox.showinfo(title, "Tick at least one.", parent=w)
        mm_targets.remember(kind, chosen)
        w.destroy()
        then(chosen)
    row = tk.Frame(w, bg=PANEL)
    row.pack(pady=14)
    ttk.Button(row, text="Post", style="Go.TButton", command=ok).pack(side="left", padx=6)
    ttk.Button(row, text="Cancel", command=w.destroy).pack(side="left", padx=6)


# ======================= Upload a video =======================
def upload_window(app):
    from tkinter import filedialog
    w = tk.Toplevel(app)
    w.title("Upload a video")
    w.configure(bg=PANEL)
    w.geometry(f"{px(620)}x{px(560)}")
    w.transient(app)
    pad = {"padx": 16, "anchor": "w"}
    tk.Label(w, text="UPLOAD A VIDEO EVERYWHERE", bg=PANEL, fg=FG, font=("Segoe UI Semibold", 12)).pack(pady=(14, 6), **pad)
    path = tk.StringVar()
    row = tk.Frame(w, bg=PANEL)
    row.pack(fill="x", padx=16)
    tk.Entry(row, textvariable=path, bg=LINE, fg=FG, insertbackground=FG, relief="flat").pack(side="left", fill="x",
                                                                                              expand=True, ipady=5)

    def browse():
        p = filedialog.askopenfilename(parent=w, title="Pick a video", filetypes=[
            ("Videos", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"), ("All files", "*.*")])
        if p:
            path.set(p)
    ttk.Button(row, text="Browse...", style="Small.TButton", command=browse).pack(side="left", padx=(6, 0))
    tk.Label(w, text="Title (leave empty: the AI writes it from the video)", bg=PANEL, fg=MUTED).pack(pady=(12, 2), **pad)
    title = tk.Entry(w, bg=LINE, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 11))
    title.pack(fill="x", padx=16, ipady=5)
    tk.Label(w, text="Description / caption (leave empty: the AI writes it)", bg=PANEL, fg=MUTED).pack(pady=(12, 2), **pad)
    desc = tk.Text(w, bg=LINE, fg=FG, insertbackground=FG, relief="flat", height=4, wrap="word", font=("Segoe UI", 11))
    desc.pack(fill="x", padx=16)
    tk.Label(w, text="Post to", bg=PANEL, fg=MUTED).pack(pady=(12, 2), **pad)
    import mm_targets
    plats = {}
    prow = tk.Frame(w, bg=PANEL)
    prow.pack(fill="x", padx=16)
    last = set(mm_targets.last("video"))
    for key in mm_targets.ready("video"):
        name = mm_targets.NAMES[key].replace(" (gets ready in the TikTok tab)", " (ready in the TikTok tab)")
        plats[key] = tk.BooleanVar(value=key in last)
        tk.Checkbutton(prow, text=name, variable=plats[key], bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL,
                       activeforeground=FG, highlightthickness=0).pack(side="left", padx=(0, 10))
    vert = tk.BooleanVar(value=True)
    tk.Checkbutton(w, text="If it's a wide video under 3 min, make it vertical (9:16, blurred background)", variable=vert,
                   bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG,
                   highlightthickness=0).pack(pady=(10, 0), **pad)
    tk.Label(w, bg=PANEL, fg=MUTED, justify="left", wraplength=px(580), text=(
        "YouTube uploads stay private until the API audit passes. Facebook Reels max 90 s. Hashtags are added "
        "automatically.")).pack(pady=(8, 0), **pad)

    def go():
        p = path.get().strip().strip('"')
        if not p or not os.path.exists(p):
            return messagebox.showinfo("Upload", "Pick a video file first.", parent=w)
        chosen = [k for k, var in plats.items() if var.get()]
        if not plats:
            return messagebox.showinfo("Upload", "No platform is connected and turned on yet (Settings -> Accounts).",
                                       parent=w)
        if not chosen:
            return messagebox.showinfo("Upload", "Tick at least one platform.", parent=w)
        mm_targets.remember("video", chosen)
        if not messagebox.askyesno("Upload", f"Post this video for REAL on {', '.join(chosen)}?", parent=w):
            return
        os.makedirs(os.path.join("media", "uploads"), exist_ok=True)
        job = os.path.join("media", "uploads", "job.json")
        with open(job, "w", encoding="utf-8") as fh:
            json.dump({"path": p, "title": title.get().strip(), "caption": desc.get("1.0", "end-1c").strip(),
                       "platforms": chosen, "vertical": bool(vert.get())}, fh)
        w.destroy()
        app.run(["machine.py", "upload-video", job], live=True, title=f"Upload: {os.path.basename(p)}",
                then=lambda: app.tiktok_tab.fill())
    ttk.Button(w, text="⬆  Upload and post", style="Go.TButton", command=go).pack(pady=16)


def custom_window(app):
    """Make content your way: an optional picture/video, optional instructions. Nothing gets posted."""
    from tkinter import filedialog
    w = tk.Toplevel(app)
    w.title("Custom content")
    w.configure(bg=PANEL)
    w.geometry(f"{px(620)}x{px(560)}")
    w.transient(app)
    pad = {"padx": 16, "anchor": "w"}
    tk.Label(w, text="CUSTOM CONTENT", bg=PANEL, fg=FG, font=("Segoe UI Semibold", 12)).pack(pady=(14, 2), **pad)
    tk.Label(w, text="Both boxes are optional. Nothing gets posted: it's saved and opened for you to look at.",
             bg=PANEL, fg=MUTED, wraplength=px(580), justify="left").pack(**pad)
    tk.Label(w, text="Picture or video (optional)", bg=PANEL, fg=MUTED).pack(pady=(12, 2), **pad)
    path = tk.StringVar()
    row = tk.Frame(w, bg=PANEL)
    row.pack(fill="x", padx=16)
    tk.Entry(row, textvariable=path, bg=LINE, fg=FG, insertbackground=FG, relief="flat").pack(side="left", fill="x",
                                                                                              expand=True, ipady=5)

    def browse():
        p = filedialog.askopenfilename(parent=w, title="Pick a picture or video", filetypes=[
            ("Pictures and videos", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff "
                                    "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"), ("All files", "*.*")])
        if p:
            path.set(p)
    ttk.Button(row, text="Browse...", style="Small.TButton", command=browse).pack(side="left", padx=(6, 0))
    ttk.Button(row, text="Clear", style="Small.TButton", command=lambda: path.set("")).pack(side="left", padx=(6, 0))
    tk.Label(w, text="What do you want? (optional)", bg=PANEL, fg=MUTED).pack(pady=(12, 2), **pad)
    desc = tk.Text(w, bg=LINE, fg=FG, insertbackground=FG, relief="flat", height=5, wrap="word", font=("Segoe UI", 11))
    desc.pack(fill="x", padx=16)
    tk.Label(w, bg=PANEL, fg=MUTED, justify="left", wraplength=px(580), text=(
        "• File + words: makes the edits you describe (\"cut it to the funny part, add captions, make it vertical\").\n"
        "• File only: the AI picks the edits.\n"
        "• Words only: the AI makes a new picture or AI video from them.\n"
        "• Both empty: something random.\n\n"
        "Crops, looks, text, trimming, speed and captions work right away. Edits that change what's IN a picture, AI "
        "restyles of videos and brand-new AI pictures/videos need ComfyUI (see the README); without it you get the "
        "closest thing it can do and a note on what to install.")).pack(pady=(10, 0), **pad)

    def go():
        p = path.get().strip().strip('"')
        if p and not os.path.exists(p):
            return messagebox.showinfo("Custom content", "Can't find that file. Pick it again or press Clear.", parent=w)
        text = desc.get("1.0", "end-1c").strip()
        force = app._game_check("Making custom content", "1-15")
        if force is None:
            return
        os.makedirs(os.path.join("media", "custom"), exist_ok=True)
        job = os.path.join("media", "custom", "job.json")
        with open(job, "w", encoding="utf-8") as fh:
            json.dump({"media": p or None, "text": text}, fh)
        what = os.path.basename(p) if p else (text[:40] if text else "random")
        w.destroy()
        app.run(["machine.py", "custom", job], title=f"Custom: {what}", force_gpu=force,
                then=lambda: app.after(300, lambda: _offer_custom_post(app, job)))
    ttk.Button(w, text="✨  Generate", style="Go.TButton", command=go).pack(pady=16)


def _offer_custom_post(app, job):
    """After Custom content is made: optionally post it (you pick where)."""
    try:
        with open(job, encoding="utf-8") as fh:
            res = json.load(fh).get("result")
    except Exception:
        res = None
    if not res or not res.get("paths"):
        return  # it failed or was stopped
    kind = res["kind"]
    if not messagebox.askyesno("Custom content", "Done! It's saved and open.\n\nPost it somewhere now?", parent=app):
        return
    cap = simpledialog.askstring("Caption", "Caption (edit it or leave it; empty = the AI writes one):",
                                 initialvalue=res.get("caption", ""), parent=app)
    if cap is None:
        return

    def go(ps):
        out = os.path.join("media", "custom", "post.json")
        if kind == "video":
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"path": res["paths"][0], "title": "", "caption": cap.strip(), "platforms": ps,
                           "vertical": True}, fh)
            app.run(["machine.py", "upload-video", out], live=True, title=f"Post custom video: {', '.join(ps)}",
                    then=lambda: app.tiktok_tab.fill())
        else:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"paths": res["paths"], "caption": cap.strip(), "platforms": ps}, fh)
            app.run(["machine.py", "post-image", out], live=True, title="Post custom picture: Instagram")
    note = "This posts for real." + (" Pictures can go to Instagram (the others need a video)." if kind == "image"
                                     else "")
    pick_platforms(app, "Post it", kind, go, note=note)


# ======================= Queue =======================
class QueueTab:
    def __init__(self, app, nb):
        self.app = app
        f = tk.Frame(nb, bg=PANEL)
        nb.add(f, text="Queue")
        self.frame = f
        bar = _bar(f)
        for t, cmd in (("▶ Preview", self.preview), ("📤 Post this clip now (all video sites)", self.post_now),
                       ("⏭ Skip this clip", self.skip), ("🗑 Drop selected idea", self.drop_idea), ("↻ Refresh", self.fill)):
            ttk.Button(bar, text=t, style="Small.TButton", command=cmd).pack(side="left", padx=(0, 6))
        tk.Label(f, text="CLIPS WAITING TO BE POSTED", bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=8)
        self.clips = _table(f, ("id", "type", "made", "posted on", "title"), (50, 80, 110, 160, 500))
        tk.Label(f, text="IDEAS IN THE QUEUE (winner remixes + fan comments)", bg=PANEL, fg=MUTED,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=8)
        self.ideas = _table(f, ("id", "kind", "for", "queued", "idea"), (50, 70, 90, 110, 600))
        self.fill()

    def fill(self):
        for tv in (self.clips, self.ideas):
            tv.delete(*tv.get_children())
        for cid, ai, made, title, path in _q(
                "SELECT c.id, COALESCE(c.ai_visual,0), replace(substr(c.created_at,6,11),'T',' '), COALESCE(c.title,''), c.path FROM mm_clips c "
                "WHERE c.status='ready' AND (SELECT COUNT(DISTINCT platform) FROM mm_posts p WHERE p.clip_id=c.id) < 3 "
                "ORDER BY c.id"):
            on = ", ".join(r[0] for r in _q("SELECT DISTINCT platform FROM mm_posts WHERE clip_id=?", (cid,))) or "-"
            self.clips.insert("", "end", iid=str(cid), values=(cid, "AI video" if ai else "your clip", made, on, title))
        for iid, kind, target, at, payload in _q("SELECT id, kind, target, substr(at,1,16), payload FROM ideas "
                                                 "WHERE status='new' ORDER BY id"):
            p = json.loads(payload)
            txt = (f"@{p.get('user')}: {p.get('comment', '')[:80]} -> {p.get('idea', '')}" if kind == "fan"
                   else p.get("text") or p.get("title") or "")
            self.ideas.insert("", "end", iid=str(iid), values=(iid, kind, target, at, txt))

    def _sel(self, tv):
        s = tv.selection()
        return s[0] if s else None

    def preview(self):
        cid = self._sel(self.clips)
        if cid:
            r = _q("SELECT path FROM mm_clips WHERE id=?", (cid,))
            if r and r[0][0] and os.path.exists(r[0][0]):
                compat.open_file(os.path.abspath(r[0][0]))
            else:
                messagebox.showinfo("Preview", "That clip's file isn't there anymore.")

    def post_now(self):
        cid = self._sel(self.clips)
        if cid:
            pick_platforms(self.app, f"Post clip {cid}", "video", lambda ps: self.app.run(
                ["machine.py", "post-clip", cid, ",".join(ps)], live=True,
                title=f"Post clip {cid}: {', '.join(ps)}", then=self.fill), note="This posts for real.")

    def skip(self):
        cid = self._sel(self.clips)
        if cid and messagebox.askyesno("Skip", f"Never post clip {cid}?"):
            c = _db()
            c.execute("UPDATE mm_clips SET status='skipped', hold_reason='you skipped it' WHERE id=?", (cid,))
            c.commit()
            c.close()
            self.fill()

    def drop_idea(self):
        iid = self._sel(self.ideas)
        if iid:
            c = _db()
            c.execute("UPDATE ideas SET status='dropped' WHERE id=?", (iid,))
            c.commit()
            c.close()
            self.fill()


# ======================= History =======================
class HistoryTab:
    def __init__(self, app, nb):
        f = tk.Frame(nb, bg=PANEL)
        nb.add(f, text="History")
        self.frame = f
        bar = _bar(f)
        self.plat = tk.StringVar(value="all")
        for p in ("all", "threads", "youtube", "tiktok", "instagram", "facebook"):
            tk.Radiobutton(bar, text=p, value=p, variable=self.plat, command=self.fill, bg=PANEL, fg=FG,
                           selectcolor=ACCENT, activebackground=PANEL, activeforeground=FG).pack(side="left")
        ttk.Button(bar, text="↻ Refresh", style="Small.TButton", command=self.fill).pack(side="right")
        self.tv = _table(f, ("when", "platform", "kind", "views", "likes", "comments", "score", "vs usual", "post"),
                         (120, 80, 100, 60, 50, 70, 60, 100, 520))
        self.fill()

    def fill(self):
        self.tv.delete(*self.tv.get_children())
        rows = [("threads", a, k, v, l, r, s, w, t) for a, k, v, l, r, s, w, t in _q(
            "SELECT created_at, kind, views, likes, replies, score, reward, text FROM posts WHERE deleted=0")]
        rows += _q("SELECT platform, posted_at, COALESCE(kind,'clip'), views, likes, "
                   "MIN(COALESCE(real_comments, comments), COALESCE(comments, 0)), score, reward, title FROM mm_posts")
        rows.sort(key=lambda r: r[1] or "", reverse=True)
        for i, (p, at, kind, v, l, cm, s, rw, t) in enumerate(rows):
            if self.plat.get() not in ("all", p):
                continue
            verdict, tag = _verdict(rw)
            self.tv.insert("", "end", iid=str(i), tags=(tag,), values=(
                (at or "")[:16].replace("T", " "), p, kind, v or 0, l or 0, cm or 0,
                f"{s:.0f}" if s is not None else "", verdict, (t or "").replace("\n", " ")[:200]))


# ======================= People =======================
class PeopleTab:
    def __init__(self, app, nb):
        f = tk.Frame(nb, bg=PANEL)
        nb.add(f, text="People")
        self.frame = f
        bar = _bar(f)
        tk.Label(bar, text="Everyone who ever commented. Double-click someone to see your history with them.",
                 bg=PANEL, fg=MUTED).pack(side="left")
        ttk.Button(bar, text="↻ Refresh", style="Small.TButton", command=self.fill).pack(side="right")
        self.tv = _table(f, ("name", "comments", "platforms", "first seen", "last seen", "what the bot knows"),
                         (160, 80, 150, 100, 100, 500))
        self.tv.bind("<Double-1>", self.show)
        self.fill()

    def fill(self):
        self.tv.delete(*self.tv.get_children())
        for who, name, n, plats, first, last, note in _q(
                "SELECT who, name, n, platforms, substr(first_seen,1,10), substr(last_seen,1,10), COALESCE(note,'') "
                "FROM people ORDER BY n DESC, last_seen DESC"):
            self.tv.insert("", "end", iid=who, values=(f"@{name}", n, ", ".join(json.loads(plats)), first, last, note))

    def show(self, _e=None):
        s = self.tv.selection()
        if not s:
            return
        r = _q("SELECT name, recent, note FROM people WHERE who=?", (s[0],))
        if not r:
            return
        name, recent, note = r[0]
        txt = (f"What the bot knows: {note or '(nothing yet)'}\n\nLast conversations:\n\n" +
               "\n\n".join(f"[{x['at']}] {x['p']}\n  @{name}: {x['said']}\n  you: {x['you']}" for x in json.loads(recent)))
        w = tk.Toplevel()
        w.title(f"@{name}")
        w.configure(bg=BG)
        t = tk.Text(w, bg=PANEL, fg=FG, wrap="word", font=(compat.mono_font(), 10), width=90, height=24, padx=10, pady=8)
        t.insert("end", txt)
        t.pack(fill="both", expand=True)


# ======================= Settings editor =======================
LABELS = {
    "source_channel_id": "Main YouTube channel ID (clips come from it)",
    "shorts_channel_id": "Shorts channel ID (set by Connect YouTube)",
    "gpu_mode": "GPU mode (auto / on / off)", "ai_video": "Autopilot makes fully-AI videos",
    "ai_videos_per_day": "Fully-AI videos per day (automatic)", "ai_quiet_minutes": "Minutes with no games before heavy AI",
    "ai_video_scenes": "Shots per AI video (1-5, each ~3 min)", "start_with_windows": "Start the app when the computer starts",
    "source_mode": "Clip sources (random / picked)", "content_mode": "What to make (own / mix / trending)", "instagram_dms": "Auto-answer Instagram DMs", "self_critique": "Self-critic grades drafts before posting",
    "instagram_stories": "Also share Instagram posts to the Story", "alerts_enabled": "Phone alerts (ntfy)",
    "review_mode": "YouTube: keep uploads private for my review", "downloads_per_run": "Videos to download per run",
    "max_downloaded_sources": "Max videos kept downloaded", "active_sources": "Videos analyzed at once",
    "delete_used_sources": "Delete downloaded videos after use", "ready_queue": "Clips to keep ready",
    "comfy_exe": "ComfyUI Desktop .exe (blank = find it)", "ollama_model": "Ollama model",
}
PLAT_LABELS = {"enabled": "On (autopilot posts here + shows in 'Post to')"}
SKIP = {"instagram_id", "ntfy_topic", "comment", "ai_video_share", "ai_min_share", "ai_max_share"}
DEFAULTS = {"self_critique": True, "ready_queue": 3, "comfy_exe": "", "ai_video": True, "ai_videos_per_day": 1,
            "ai_quiet_minutes": 90, "ai_video_scenes": 3, "start_with_windows": sys.platform != "darwin", "source_mode": "random"}


def settings_window(root):
    try:
        with open(CFG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return messagebox.showerror("Settings", f"Couldn't read {CFG}: {e}")
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    w = tk.Toplevel(root)
    w.title("Clip Machine settings")
    w.configure(bg=BG)
    w.geometry("760x720")
    canvas = tk.Canvas(w, bg=BG, highlightthickness=0)
    sb = ttk.Scrollbar(w, command=canvas.yview)
    inner = tk.Frame(canvas, bg=BG)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    canvas.pack(fill="both", expand=True)
    fields = []

    def row(path, val, label, indent=0):
        fr = tk.Frame(inner, bg=BG)
        fr.pack(fill="x", padx=(14 + indent, 14), pady=3)
        tk.Label(fr, text=label, bg=BG, fg=FG, width=44, anchor="w").pack(side="left")
        if isinstance(val, bool):
            var = tk.BooleanVar(value=val)
            tk.Checkbutton(fr, variable=var, bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                           selectcolor=PANEL, text="on").pack(side="left")
        else:
            var = tk.StringVar(value="" if val is None else str(val))
            tk.Entry(fr, textvariable=var, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", width=24).pack(side="left")
        fields.append((path, val, var))

    tk.Label(inner, text="GENERAL", bg=BG, fg=ACCENT, font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=14, pady=(12, 2))
    for k, v in cfg.items():
        if k in SKIP or k.startswith("_") or k.endswith("help") or isinstance(v, (dict, list)):
            continue
        row((k,), v, LABELS.get(k, k.replace("_", " ")))
    for plat, pc in (cfg.get("platforms") or {}).items():
        tk.Label(inner, text=plat.upper(), bg=BG, fg=ACCENT, font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=14, pady=(12, 2))
        for k, v in pc.items():
            if not isinstance(v, (dict, list)) and not k.endswith("help"):
                row(("platforms", plat, k), v, PLAT_LABELS.get(k, k.replace("_", " ")), indent=10)

    def save():
        for path, old, var in fields:
            raw = var.get()
            if isinstance(old, bool):
                new = bool(raw)
            elif isinstance(old, (int, float)) and not isinstance(old, bool):
                try:
                    new = int(raw) if isinstance(old, int) and str(raw).strip().lstrip("-").isdigit() else float(raw)
                except ValueError:
                    return messagebox.showerror("Settings", f"'{raw}' isn't a number ({'/'.join(path)})")
            else:
                new = raw
            d = cfg
            for p in path[:-1]:
                d = d[p]
            d[path[-1]] = new
        with open(CFG + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(CFG + ".tmp", CFG)
        messagebox.showinfo("Settings", "Saved. The bot uses them from its next run.")
        w.destroy()

    ttk.Button(inner, text="💾  Save settings", style="Big.TButton", command=save).pack(fill="x", padx=14, pady=16)


# ======================= Series editor =======================
def series_window(root):
    import mm_content
    c = _db()
    mm_content.ensure(c)
    c.close()
    w = tk.Toplevel(root)
    w.title("Recurring series")
    w.configure(bg=BG)
    w.geometry("900x420")
    bar = _bar(w)
    tv = _table(w, ("id", "on", "episodes", "made by", "name", "what each episode is"), (40, 40, 70, 70, 180, 480))

    def fill():
        tv.delete(*tv.get_children())
        for sid, name, about, ep, active, by in _q("SELECT id, name, about, episode, active, made_by FROM series ORDER BY id"):
            tv.insert("", "end", iid=str(sid), values=(sid, "yes" if active else "no", ep, by, name, about),
                      tags=(() if active else ("muted",)))

    def toggle():
        s = tv.selection()
        if s:
            c = _db()
            c.execute("UPDATE series SET active=1-active WHERE id=?", (s[0],))
            c.commit()
            c.close()
            fill()

    def add():
        name = simpledialog.askstring("New series", "Series name (e.g. 'Ranked Rage Diary'):", parent=w)
        if not name:
            return
        about = simpledialog.askstring("New series", "What happens in each episode?", parent=w)
        if about:
            c = _db()
            c.execute("INSERT OR IGNORE INTO series (name, about, made_by) VALUES (?,?, 'you')", (name[:40], about[:300]))
            c.commit()
            c.close()
            fill()

    ttk.Button(bar, text="➕ New series", style="Small.TButton", command=add).pack(side="left", padx=(0, 6))
    ttk.Button(bar, text="⏯ Turn selected on/off", style="Small.TButton", command=toggle).pack(side="left")
    fill()
