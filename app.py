"""
CLIP MACHINE - desktop app.
Start it with windows\\Clip Machine.vbs (no console window), "Clip Machine.command" on a Mac, or: python app.py
Every button runs the same python commands the bot runs on its own; what they print shows in the Output tab.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
import compat  # noqa: E402  (Windows/Mac helpers)
import creator_profile as profile  # noqa: E402  (profile.json: creator name, handles, lore)
compat.load_env()
PY = sys.executable.replace("pythonw.exe", "python.exe")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TASK = "ClipMachine Autopilot"
try:  # sharp text on scaled (125%/150%) Windows screens instead of blurry
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import app_views as views  # noqa: E402
from app_views import px, set_text  # noqa: E402

BG, PANEL, FG, MUTED, ACCENT, GOOD, BAD = "#14111c", "#1f1a2b", "#ece8f6", "#a59fb8", "#8b45ff", "#3ecf8e", "#ff5c7a"
LINE, WARN = "#2c2540", "#ffb454"
MONO = (compat.mono_font(), 10)
PLATFORMS = (("threads", "Threads"), ("youtube", "YouTube"), ("tiktok", "TikTok"), ("instagram", "Instagram"),
             ("facebook", "Facebook"))


def env(live=False, force_gpu=False):
    e = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", DRY_RUN="0" if live else "1",
             BOT_INTERACTIVE="1")  # you started it from the app, so it runs even if a game is open
    if force_gpu:
        e["BOT_FORCE_GPU"] = "1"  # you OK'd it mid-game: AI pictures/video/music run too until it finishes
    e.setdefault("BOT_MODEL", "qwen3:8b")
    return e


def read(path, tail=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if tail:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - tail))
            return f.read()
    except OSError:
        return ""


PORT = 47653  # only one Clip Machine window at a time: a second launch just brings the first one to the front


def already_running():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5) as c:
            c.sendall(b"show")
        return True
    except OSError:
        return False


def set_autostart(on):
    """Start with Windows (a normal per-user startup entry, same as ticking 'run at startup' in any app)."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
        try:
            if on:
                pyw = sys.executable.replace("python.exe", "pythonw.exe")
                pyw = pyw if os.path.exists(pyw) else sys.executable
                winreg.SetValueEx(key, "ClipMachine", 0, winreg.REG_SZ,
                                  f'"{pyw}" "{os.path.join(HERE, "app.py")}" --startup')
            else:
                try:
                    winreg.DeleteValue(key, "ClipMachine")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def autostart_wanted():
    try:
        return bool(json.loads(read("machine_config.json") or "{}").get("start_with_windows", True))
    except Exception:
        return True


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            views.SCALE[0] = min(3.0, max(1.0, self.winfo_fpixels("1i") / 96.0))
        except Exception:
            pass
        self.title("Clip Machine")
        w, h = min(px(1280), self.winfo_screenwidth() - 60), min(px(820), self.winfo_screenheight() - 100)
        self.geometry(f"{w}x{h}")
        self.minsize(min(px(980), w), min(px(600), h))
        self.configure(bg=BG)
        try:
            self.iconphoto(True, tk.PhotoImage(file=os.path.join(HERE, "icon.png")))
        except Exception:
            pass
        self.q = queue.Queue()
        self.proc = None
        self.chat_proc = None
        self._then = None
        self._started = 0.0
        self._label = ""
        self._autopilot_on = None
        self._refreshing = False
        self._style()
        self._copy_bindings()
        self._build()
        self.bind("<F5>", lambda e: self.refresh(once=True))
        self._listen()
        try:  # a leftover from a crash: back to "paused while you play"
            os.remove(os.path.join(HERE, "gpu_override.txt"))
        except OSError:
            pass
        threading.Thread(target=lambda: set_autostart(autostart_wanted()), daemon=True).start()
        if compat.IS_WIN:
            threading.Thread(target=lambda: subprocess.run(["schtasks", "/delete", "/tn", "ClipMachine Replies", "/f"],
                                                           capture_output=True, creationflags=NOWIN), daemon=True).start()
        if "--startup" in sys.argv:  # started with Windows: stay out of the way in the taskbar
            self.after(200, self.iconify)
        self.after(100, self._pump)
        self.after(300, self.refresh)
        self.after(1000, self._clock)

    def _listen(self):
        """A second launch pokes this port; we come to the front instead of opening twice."""
        import socket
        try:
            srv = socket.socket()
            srv.bind(("127.0.0.1", PORT))
            srv.listen(2)
        except OSError:
            return

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    conn.close()
                    self.after(0, self._to_front)
                except OSError:
                    return
        threading.Thread(target=loop, daemon=True).start()

    def _to_front(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))

    # ---------- look ----------
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0), bordercolor=BG, lightcolor=BG,
                    darkcolor=BG)
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(13, 7), borderwidth=0, bordercolor=BG,
                    lightcolor=PANEL, darkcolor=PANEL, focuscolor=PANEL)
        s.map("TNotebook.Tab", background=[("selected", ACCENT), ("active", LINE)],
              foreground=[("selected", "white"), ("active", FG)])
        s.configure("Side.TButton", background=PANEL, foreground=FG, padding=(12, 9), anchor="w", borderwidth=0)
        s.map("Side.TButton", background=[("active", LINE)])
        s.configure("Big.TButton", background=ACCENT, foreground="white", padding=(12, 12), anchor="w",
                    font=("Segoe UI Semibold", 11), borderwidth=0)
        s.map("Big.TButton", background=[("active", "#a46bff")])
        s.configure("Stop.TButton", background="#3a1a24", foreground=BAD, padding=(12, 9), anchor="w", borderwidth=0)
        s.map("Stop.TButton", background=[("active", "#56222f"), ("disabled", PANEL)], foreground=[("disabled", "#5d566e")])
        s.configure("TLabel", background=BG, foreground=FG)
        for name in ("TScrollbar", "Vertical.TScrollbar"):
            s.configure(name, background="#3a3154", troughcolor=PANEL, bordercolor=PANEL, arrowcolor=MUTED,
                        lightcolor="#3a3154", darkcolor="#3a3154", gripcount=0, relief="flat")
            s.map(name, background=[("active", ACCENT), ("disabled", PANEL)])
        s.map("TNotebook.Tab", expand=[("selected", (0, 0, 0, 0))])
        views.style(self)

    def _chip(self, parent, text="", click=None):
        lb = tk.Label(parent, text=text, font=("Segoe UI Semibold", 9), bg=PANEL, fg=MUTED, padx=11, pady=5)
        lb.pack(side="right", padx=(8, 0))
        if click:
            lb.configure(cursor="hand2")
            lb.bind("<Button-1>", lambda e: click())
        return lb

    def _build(self):
        self.status = tk.Label(self, text="Ready.", bg=PANEL, fg=MUTED, anchor="w", padx=12, pady=4)
        self.status.pack(fill="x", side="bottom")  # packed first so it never gets squeezed off the window

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(top, text=profile.name().upper()[:14], font=("Segoe UI Black", 19), bg=BG, fg=FG).pack(side="left")
        tk.Label(top, text=" MACHINE", font=("Segoe UI Black", 19), bg=BG, fg=ACCENT).pack(side="left")
        self.pill = self._chip(top, "Autopilot: checking...", self.toggle_autopilot)
        self.game_chip = self._chip(top, "")
        self.gpu_chip = self._chip(top, "", self.toggle_game_detect)

        self.today = tk.Label(self, text="", bg=BG, fg=MUTED, anchor="w", font=("Segoe UI", 10))
        self.today.pack(fill="x", padx=18, pady=(0, 6))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        side = tk.Frame(body, bg=BG, width=px(250))
        side.pack(side="left", fill="y", padx=(0, 12))
        side.pack_propagate(False)

        def btn(text, cmd, style="Side.TButton", parent=side, **pack):
            b = ttk.Button(parent, text=text, command=cmd, style=style)
            b.pack(fill="x", pady=2, **pack)
            return b

        def head(text):
            tk.Label(side, text=text, bg=BG, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(12, 2))

        btn("🤖  FULL AI CONTROL", self.full_ai, "Big.TButton")
        btn("🎞  Make a fully-AI video", self.ai_video)
        btn("⬆  Upload a video everywhere", lambda: views.upload_window(self))
        head("POST & MAKE")
        btn("📤  Post now...", self.post_menu)
        btn("🎬  Make content (no posting)...", self.make_menu)
        btn("✨  Custom: upload + describe...", lambda: views.custom_window(self))
        btn("📥  Get more of my videos", self.fetch)
        head("WHAT TO MAKE")
        self.mode_names = ("My stuff", "Mix", "Trending (none of mine)")
        self.mode_keys = ("own", "mix", "trending")
        try:
            cur = json.loads(read("machine_config.json") or "{}").get("content_mode", "mix")
        except Exception:
            cur = "mix"
        self.mode_var = tk.IntVar(value=self.mode_keys.index(cur) if cur in self.mode_keys else 1)
        self.mode_label = tk.Label(side, text="", bg=BG, fg=FG, font=("Segoe UI Semibold", 9), wraplength=px(240),
                                   justify="left")
        self.mode_label.pack(anchor="w")
        sc = tk.Scale(side, from_=0, to=2, orient="horizontal", showvalue=False, variable=self.mode_var, resolution=1,
                      command=lambda v: self._mode_moved(), bg=ACCENT, troughcolor=LINE, highlightthickness=0,
                      activebackground="#a46bff", sliderrelief="flat", borderwidth=0, length=px(230),
                      sliderlength=px(34), width=px(14))
        sc.pack(fill="x", pady=(2, 0))
        ends = tk.Frame(side, bg=BG)
        ends.pack(fill="x")
        for i, t in enumerate(("Mine", "Mix", "Trending")):
            tk.Label(ends, text=t, bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(
                side=("left", "top", "right")[i] if i != 1 else "left", expand=i == 1)
        sc.bind("<ButtonRelease-1>", lambda e: self._mode_save())
        self._mode_moved()
        head("RUN")
        btn("▶  Run one autopilot cycle", lambda: self.run(["machine.py", "tick"], live=True,
                                                          title="One autopilot cycle"))
        btn("💬  Answer comments now", lambda: self.run(["machine.py", "replies"], live=True, title="Answer comments"))
        btn("📊  Status report", lambda: self.run(["machine.py", "status"], title="Status"))
        bottom = tk.Frame(side, bg=BG)
        bottom.pack(side="bottom", fill="x")
        btn("⚙  Settings...", self.settings_menu, parent=bottom)
        self.stop_btn = btn("⏹  Stop current task", self.stop, "Stop.TButton", parent=bottom)
        self.stop_btn.state(["disabled"])
        when = tk.Frame(bottom, bg=BG)
        when.pack(fill="x", pady=(6, 0))
        tk.Label(when, text="When the task finishes:", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        self.after_done = tk.StringVar(value="do nothing")
        om = tk.OptionMenu(when, self.after_done, "do nothing", "sleep the PC", "shut down the PC",
                           command=lambda v: self._after_done_changed())
        om.configure(bg=PANEL, fg=FG, activebackground=LINE, activeforeground=FG, relief="flat", highlightthickness=0,
                     font=("Segoe UI", 9), borderwidth=0, indicatoron=True)
        om["menu"].configure(bg=PANEL, fg=FG, activebackground=ACCENT, activeforeground="white", font=("Segoe UI", 10))
        om.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.after_menu = om

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.out = self._textbox("Output")
        for tag, color in (("good", GOOD), ("bad", BAD), ("muted", MUTED), ("head", ACCENT)):
            self.out.tag_configure(tag, foreground=color)
        set_text(self.out, "Whatever you start from the buttons on the left shows up here.\n\n"
                           "The autopilot runs on its own in the background (see the Bot log tab).")
        self.tiktok_tab = views.TikTokTab(self, self.tabs)
        self.videos_tab = views.VideosTab(self, self.tabs)
        self.queue_tab = views.QueueTab(self, self.tabs)
        self.held_tab = views.HeldTab(self, self.tabs)
        self.history_tab = views.HistoryTab(self, self.tabs)
        self.board = self._textbox("Scoreboard")
        self.brain = self._textbox("Brain")
        self.people_tab = views.PeopleTab(self, self.tabs)
        self.health = self._textbox("Health")
        self.log = self._textbox("Bot log")
        for box in (self.health,):
            box.tag_configure("ok", foreground=GOOD)
            box.tag_configure("bad", foreground=BAD)
            box.tag_configure("muted", foreground=MUTED)
            box.tag_configure("head", foreground=ACCENT, font=(compat.mono_font(), 11, "bold"))

        chat = tk.Frame(self.tabs, bg=PANEL)
        self.chat_tab = chat
        self.tabs.add(chat, text="Chat")
        row = tk.Frame(chat, bg=PANEL)
        row.pack(fill="x", side="bottom", padx=8, pady=(0, 8))
        tk.Label(chat, text="Talk normally: ask how things are going, or tell it what to post, make or change. "
                            "It asks before posting anything.", bg=PANEL, fg=MUTED).pack(side="bottom", anchor="w", padx=8,
                                                                                         pady=(0, 4))
        self.chat_box = self._text(chat)
        self.chat_box.configure(state="disabled", font=("Segoe UI", 11), spacing3=4)
        self.chat_box.tag_configure("you", foreground=ACCENT, font=("Segoe UI Semibold", 11))
        self.chat_box.tag_configure("muted", foreground=MUTED)
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.chat_in = tk.Entry(row, bg=LINE, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 11))
        self.chat_in.pack(side="left", fill="x", expand=True, ipady=7)
        self.chat_in.bind("<Return>", lambda e: self.send_chat())
        ttk.Button(row, text="Send", style="Go.TButton", command=self.send_chat).pack(side="left", padx=(6, 0))
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab)

    def _on_tab(self, _e=None):
        """Tables refresh when you open their tab (not on a timer, so your selection stays put)."""
        cur = self.tabs.select()
        for tab in (self.tiktok_tab, self.videos_tab, self.queue_tab, self.held_tab, self.history_tab, self.people_tab):
            if str(tab.frame) == cur:
                try:
                    tab.fill()
                except Exception as e:
                    self.status.config(text=f"Couldn't load that tab: {e}", fg=BAD)
        if cur != str(self.tiktok_tab.frame):
            self.tiktok_tab.stop_preview()
        if cur == str(self.chat_tab):
            self.open_chat()

    def _text(self, parent):
        return tk.Text(parent, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", wrap="word", font=MONO,
                       padx=12, pady=10, selectbackground=ACCENT, selectforeground="white", highlightthickness=0)

    # ---------- copying text (every text box: select + Cmd/Ctrl+C, Cmd/Ctrl+A, right-click menu) ----------
    def _copy_bindings(self):
        def focus(e):
            e.widget.focus_set()  # read-only boxes don't take focus on click by themselves (Mac), so Cmd+C did nothing

        def copy(e):
            w = e.widget
            try:
                txt = w.get("sel.first", "sel.last")
            except tk.TclError:
                return "break"
            self.clipboard_clear()
            self.clipboard_append(txt)
            return "break"

        def select_all(e):
            e.widget.tag_add("sel", "1.0", "end-1c")
            return "break"

        def menu(e):
            w = e.widget
            w.focus_set()
            m = tk.Menu(self, tearoff=0)
            m.add_command(label="Copy", command=lambda: copy(types_ns(w)))
            m.add_command(label="Copy all", command=lambda: self.copy_all(w))
            m.add_command(label="Select all", command=lambda: select_all(types_ns(w)))
            m.tk_popup(e.x_root, e.y_root)

        def types_ns(w):
            return type("E", (), {"widget": w})()

        self.bind_class("Text", "<Button-1>", focus, add="+")
        for mod in ("Command", "Control"):
            self.bind_class("Text", f"<{mod}-c>", copy)
            self.bind_class("Text", f"<{mod}-C>", copy)
            self.bind_class("Text", f"<{mod}-a>", select_all)
        for b in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.bind_class("Text", b, menu)

    def copy_all(self, box):
        self.clipboard_clear()
        self.clipboard_append(box.get("1.0", "end-1c"))

    def _textbox(self, name):
        f = tk.Frame(self.tabs, bg=PANEL)
        self.tabs.add(f, text=name)
        t = self._text(f)
        t.configure(state="disabled")  # read-only, but you can still select + copy
        sb = ttk.Scrollbar(f, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        btn = ttk.Button(f, text="Copy all")
        btn.configure(command=lambda: (self.copy_all(t), btn.configure(text="Copied ✓"),
                                       self.after(1500, lambda: btn.configure(text="Copy all"))))
        btn.place(relx=1.0, x=-24, y=6, anchor="ne")
        return t

    def _append(self, box, text, tag=None):
        box.configure(state="normal")
        box.insert("end", text, tag or ())
        box.configure(state="disabled")
        box.see("end")

    # ---------- running commands ----------
    def busy(self):
        return self._started > 0

    def run(self, args, live=False, stdin_text=None, title=None, then=None, force_gpu=False):
        if self.busy():
            messagebox.showinfo("Busy", f"Still running: {self._label}\n\nWait for it or press Stop.")
            return
        self.tabs.select(0)
        self._label = title or " ".join(args)
        self.out._shown = None
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")
        self._append(self.out, f"▶ {self._label}{'  (LIVE)' if live else '  (test run: nothing gets posted)'}\n\n", "head")
        self._then = then
        self._started = time.time()
        self.stop_btn.state(["!disabled"])

        def work():
            try:
                if force_gpu:  # tells the background autopilot not to unload the AI mid-job
                    with open(os.path.join(HERE, "gpu_override.txt"), "w") as fh:
                        fh.write(str(time.time()))
                self.proc = subprocess.Popen([PY] + args, cwd=HERE, env=env(live, force_gpu), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             encoding="utf-8", errors="replace", creationflags=NOWIN)
                if stdin_text is not None:
                    self.proc.stdin.write(stdin_text)
                    self.proc.stdin.flush()
                self.proc.stdin.close()
                for line in self.proc.stdout:
                    self.q.put(("out", line))
                self.q.put(("done", self.proc.wait()))
            except Exception as e:
                self.q.put(("out", f"\n[couldn't start: {e}]\n"))
                self.q.put(("done", 1))
            finally:
                if force_gpu:  # job over: back to "paused while you play"
                    try:
                        os.remove(os.path.join(HERE, "gpu_override.txt"))
                    except OSError:
                        pass
        self.proc = None
        self._started = time.time()
        threading.Thread(target=work, daemon=True).start()

    def _after_done_changed(self):
        v = self.after_done.get()
        self.after_menu.configure(fg=WARN if v != "do nothing" else FG)
        if v != "do nothing" and not self.busy():
            self.status.config(text=f"The PC will {v.split(' the')[0]} after the next task you start finishes.", fg=WARN)

    def _power_off(self, action, secs=60):
        """Countdown with a Cancel button, then sleep or shut down."""
        win = tk.Toplevel(self)
        win.title("Clip Machine")
        win.configure(bg=PANEL)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        verb = "go to sleep" if action == "sleep the PC" else "shut down"
        lb = tk.Label(win, text="", bg=PANEL, fg=FG, font=("Segoe UI Semibold", 12), padx=24, pady=18)
        lb.pack()
        state = {"left": secs, "cancel": False}

        def cancel():
            state["cancel"] = True
            win.destroy()
            self.status.config(text=f"Cancelled: the PC won't {verb}.", fg=MUTED)
        ttk.Button(win, text="Cancel", style="Go.TButton", command=cancel).pack(pady=(0, 16))
        win.protocol("WM_DELETE_WINDOW", cancel)

        def tick():
            if state["cancel"]:
                return
            if state["left"] <= 0:
                win.destroy()
                self.tiktok_tab.stop_preview()
                if action == "sleep the PC":
                    compat.sleep_pc()
                else:
                    compat.shutdown_pc()
                return
            lb.config(text=f"Task finished. Your PC will {verb} in {state['left']} seconds.")
            state["left"] -= 1
            win.after(1000, tick)
        tick()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self._stopped = True
            self.proc.kill()
            self.q.put(("out", "\n[stopped]\n"))

    @staticmethod
    def _tone(line):
        low = line.lower()
        if any(w in low for w in ("traceback", "error", "failed", "couldn't", "could not", "[stopped]", "blocked")):
            return "bad"
        if any(w in low for w in ("posted", "uploaded", "saved", "sent to", "ready", "copied")) and "not posting" not in low:
            return "good"
        if low.startswith(("[", "...", "#", "=")) or "not posting" in low:
            return "muted"
        return None

    def _pump(self):
        try:
            for _ in range(400):
                kind, val = self.q.get_nowait()
                if kind == "out":
                    self._append(self.out, val, self._tone(val))
                elif kind == "done":
                    ok = val == 0
                    took = int(time.time() - self._started)
                    self._started = 0.0
                    self.stop_btn.state(["disabled"])
                    self.status.config(text=(f"Done: {self._label} ({took}s)" if ok else
                                             f"{self._label} stopped with an error (code {val}). See the Output tab."),
                                       fg=GOOD if ok else BAD)
                    action = self.after_done.get()
                    if action != "do nothing":  # one time only: it resets so the next task doesn't sleep the PC too
                        self.after_done.set("do nothing")
                        self._after_done_changed()
                        if getattr(self, "_stopped", False):
                            self.status.config(text="You stopped the task, so the PC stays on.", fg=MUTED)
                        else:
                            self.after(500, lambda a=action: self._power_off(a))
                    self._stopped = False
                    then, self._then = self._then, None
                    if then:
                        try:
                            then()
                        except Exception:
                            pass
                    self.refresh(once=True)
                elif kind == "chat":
                    self._append(self.chat_box, val)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _clock(self):
        if self._started:
            s = int(time.time() - self._started)
            then = self.after_done.get()
            self.status.config(text=f"Running: {self._label}   {s // 60}:{s % 60:02d}"
                               + (f"   ·   then: {then.split(' the')[0]}" if then != "do nothing" else ""), fg=ACCENT)
        self.after(1000, self._clock)

    # ---------- background info: autopilot, game pause, today's posts, scoreboard, brain, health, log ----------
    def refresh(self, once=False):
        if not self._refreshing:
            self._refreshing = True
            threading.Thread(target=self._gather, daemon=True).start()
        if not once:
            self.after(30000, self.refresh)

    refresh_side = refresh  # old name, still used by the views

    def _ram_guard(self):
        """Safety net: if the chat helper (or anything it started) holds over 6 GB while it's just sitting there,
        restart it so the RAM goes back to Windows. Jobs started from the buttons are left alone."""
        try:
            import psutil
            p = self.chat_proc
            if not (p and p.poll() is None):
                return
            root = psutil.Process(p.pid)
            kids = root.children(recursive=True)
            if kids:  # a job is running right now
                return
            gb = root.memory_info().rss / 1e9
            if gb > 6:
                p.kill()
                self.q.put(("chat", f"\n[the chat was holding {gb:.1f} GB of RAM while idle, so it was restarted]\n"))
        except Exception:
            pass

    def _gather(self):
        d = {}
        self._ram_guard()
        try:
            try:
                d["autopilot"] = compat.autopilot_status(TASK)
            except Exception:
                d["autopilot"] = None
            try:
                d["cfg"] = json.loads(read("machine_config.json") or "{}")
            except Exception:
                d["cfg"] = {}
            try:
                import mm_gpu
                d["paused"], d["why"] = mm_gpu.game_pause()
                d["heavy"] = mm_gpu.heavy_ok()
            except Exception:
                d["paused"], d["why"] = False, ""
            try:
                d["health"] = json.loads(read("health.json") or "{}")
            except Exception:
                d["health"] = {}
            d["board"] = read("scoreboard.md")
            d["log"] = read("bot.log", tail=40000)
            try:
                import sqlite3
                from datetime import datetime, timezone
                con = sqlite3.connect("threadsbot.db", timeout=10)
                since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) \
                    .astimezone(timezone.utc).isoformat()
                counts = dict(con.execute("SELECT platform, COUNT(*) FROM mm_posts WHERE posted_at > ? GROUP BY platform",
                                          (since,)).fetchall())
                counts["threads"] = con.execute("SELECT COUNT(*) FROM posts WHERE media_id IS NOT NULL AND created_at > ?",
                                                (since,)).fetchone()[0]
                d["counts"] = counts
                try:
                    import mm_brain
                    d["brain"] = mm_brain.report(con)
                except Exception as e:
                    d["brain"] = f"(brain info unavailable: {e})"
                con.close()
            except Exception:
                d["counts"] = {}
            tok = {f: read(f) for f in ("youtube_token.json", "tiktok_token.json")}
            d["checks"] = [
                ("Threads token", bool(os.environ.get("THREADS_TOKEN"))),
                ("YouTube connected", bool(tok["youtube_token.json"])),
                ("YouTube comment replies allowed", "force-ssl" in tok["youtube_token.json"]),
                ("TikTok connected", bool(tok["tiktok_token.json"])),
                ("TikTok stats allowed", "video.list" in tok["tiktok_token.json"]),
                ("Instagram connected", os.path.exists("instagram_token.json")),
                ("Phone alerts (ntfy) topic", bool(d["cfg"].get("ntfy_topic"))),
                ("Voice clone installed", os.path.exists(compat.venv_python("voice_env"))),
                ("Voice sample (voice_ref.wav)", os.path.exists("voice_ref.wav")),
            ]
        finally:
            self.after(0, lambda: self._apply(d))

    def _apply(self, d):
        self._refreshing = False
        on = d.get("autopilot")
        if on is not None:
            self._autopilot_on = on
            self.pill.config(text="● Autopilot ON" if on else "○ Autopilot OFF  (click to turn on)",
                             bg="#123d2c" if on else "#4a1a26", fg=GOOD if on else BAD)
        cfg = d.get("cfg") or {}
        mode = cfg.get("gpu_mode", "auto")
        self.gpu_chip.config(text={"auto": "🎮 Game detection: ON", "on": "🎮 Game detection: OFF (always runs)",
                                   "off": "GPU: off"}.get(mode, f"GPU: {mode}"),
                             fg=MUTED if mode == "auto" else WARN)
        if d.get("paused"):
            self.game_chip.config(text="⚡ Running anyway (you OK'd it)" if os.path.exists("gpu_override.txt") and self.busy()
                                  else "⏸ Paused while you play", bg="#4a3a12", fg=WARN)
        else:
            heavy = d.get("heavy") or (True, "")
            self.game_chip.config(text=("" if not on else "AI free to work" if heavy[0] else
                                        "AI video waits for 90 min without games"), bg=PANEL if on else BG, fg=MUTED)
        counts, plats = d.get("counts") or {}, cfg.get("platforms") or {}
        bits = []
        for key, name in PLATFORMS:
            p = plats.get(key, {})
            if key != "threads" and not p.get("enabled"):
                continue
            cap = p.get("per_day" if key == "threads" else "max_per_day")
            bits.append(f"{name} {counts.get(key, 0)}" + (f"/{cap}" if cap else ""))
        self.today.config(text="Posted today:   " + "   ·   ".join(bits) if bits else "")
        set_text(self.board, d.get("board") or "(nothing yet: it appears after the first autopilot run)")
        set_text(self.log, d.get("log") or "(nothing yet: it appears after the first autopilot run)", keep="end")
        if not getattr(self.log, "_seen", False):
            self.log.see("end")
            self.log._seen = True
        set_text(self.brain, "HOW THE BOT THINKS (mood, what's working, its decisions, lessons, the people it remembers)\n\n"
                 + (d.get("brain") or "(nothing yet)"))
        self._fill_health(d.get("health") or {}, d.get("checks") or [], cfg)
        try:
            if self.tabs.select() != str(self.tiktok_tab.frame):
                self.tiktok_tab.fill()  # keeps the "TikTok (3)" count on the tab fresh
        except Exception:
            pass

    def _fill_health(self, h, checks, cfg):
        """Green/red per part of the bot, from health.json (written every autopilot run) + setup checks."""
        key = json.dumps([h, checks], sort_keys=True, default=str)
        box = self.health
        if getattr(box, "_shown", None) == key:
            return
        box._shown = key
        at = box.yview()[0]
        box.configure(state="normal")
        box.delete("1.0", "end")
        bad = sorted(n for n, e in h.items() if e.get("last") != "ok")
        box.insert("end", "PARTS OF THE BOT (last autopilot runs)\n", "head")
        if not h:
            box.insert("end", "  (fills in after the next autopilot run)\n", "muted")
        for name in bad + sorted(n for n in h if n not in bad):  # problems first
            e = h[name]
            ok = e.get("last") == "ok"
            box.insert("end", "  ● ", "ok" if ok else "bad")
            box.insert("end", f"{name:<18}")
            if ok:
                box.insert("end", f" ok {e.get('ok_at', '')}" + (f" ({e.get('secs')}s)" if e.get("secs", 0) >= 1 else "")
                           + "\n", "muted")
            else:
                box.insert("end", f" ERROR {e.get('err_at', '')}: {e.get('err', '')[:200]}\n", "bad")
        box.insert("end", "\nCONNECTIONS & EXTRAS\n", "head")
        for label, ok in checks:
            box.insert("end", "  ● ", "ok" if ok else "bad")
            box.insert("end", f"{label}\n")
        if cfg.get("ntfy_topic"):
            box.insert("end", f"\n  Your ntfy topic: {cfg['ntfy_topic']}\n", "muted")
        box.configure(state="disabled")
        box.yview_moveto(at)
        try:
            self.tabs.tab(box.master, text=f"Health ({len(bad)})" if bad else "Health")
        except Exception:
            pass

    # ---------- actions ----------
    def confirm(self, what):
        return messagebox.askyesno("Post for real?", f"This posts for REAL on {what}. Go?")

    def toggle_game_detect(self):
        """Click the chip: ON = pause while you play (default). OFF = never check for games, always run."""
        try:
            mode = json.loads(read("machine_config.json") or "{}").get("gpu_mode", "auto")
        except Exception:
            mode = "auto"
        if mode == "auto":
            if not messagebox.askyesno("Game detection", "Turn game detection OFF?\n\nThe bot stops checking for games "
                                       "and just runs everything on its schedule, AI videos included. Your games may "
                                       "lag while it works.\n\nClick the chip again any time to turn it back on."):
                return
            new = "on"
        else:
            new = "auto"
        self.run(["machine.py", "gpu", new], title="Game detection " + ("OFF: always runs" if new == "on" else
                                                                         "ON: pauses while you play"),
                 then=lambda: self.refresh(once=True))

    def _game_check(self, what, minutes):
        """None = cancel. Otherwise whether to override 'paused while you play' for this one job."""
        try:
            import mm_gpu
            if json.loads(read("machine_config.json") or "{}").get("gpu_mode", "auto") == "on":
                return False  # game detection is off: just run
            games = sorted(mm_gpu.running_games())
        except Exception:
            games = []
        if not games:
            return False
        ok = messagebox.askyesno(
            f"{what}: a game is running",
            f"You're playing {', '.join(games)}.\n\n{what} uses your graphics card hard for about {minutes} minutes, "
            "so your game may lag or stutter until it's done.\n\nRun it anyway? (It ignores 'paused while you play' "
            "just for this job, then goes back to normal.)", icon="warning")
        return True if ok else None

    def full_ai(self):
        force = self._game_check("FULL AI CONTROL", "5-15")
        if force is None:
            return
        if force or messagebox.askyesno("FULL AI CONTROL", "The bot decides what to post and posts on Threads, YouTube, "
                                        "Instagram and Facebook right now, and gets a TikTok ready. Let it loose?"):
            self.run(["machine.py", "full-ai"], live=True, title="FULL AI CONTROL", force_gpu=force)

    def ai_video(self):
        force = self._game_check("Making a fully-AI video", "10-15")
        if force is None:
            return
        topic = simpledialog.askstring(
            "Make a fully-AI video", "What should it be about?\n\nLeave it empty and the bot picks from what's trending "
            "in gaming and popular memes right now.\n\nEverything is AI-made (pictures, animation, music, your AI voice). "
            "It takes roughly 10 minutes.", parent=self)
        if topic is None:
            return
        self.run(["machine.py", "ai-video"] + topic.split(), live=True, title="Make a fully-AI video",
                 then=lambda: self.queue_tab.fill(), force_gpu=force)

    def _menu(self, items, parent=None):
        m = tk.Menu(parent or self, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT, activeforeground="white",
                    font=("Segoe UI", 10), borderwidth=0)
        for label, fn in items:
            if label == "-":
                m.add_separator()
            elif isinstance(fn, list):
                m.add_cascade(label=label, menu=self._menu(fn, m))
            else:
                m.add_command(label=label, command=fn)
        return m

    def _popup(self, items):
        self._menu(items).tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def post_menu(self):
        def go(t, name):
            if self.confirm(name):
                self.run(["machine.py", "force", t], live=True, title=f"Post now: {name}")
        self._popup([("Threads", lambda: go("threads", "Threads")),
                     ("YouTube Short", lambda: go("youtube", "YouTube")),
                     ("Instagram", lambda: go("instagram", "Instagram")),
                     ("Facebook Reel", lambda: go("facebook", "Facebook")),
                     ("TikTok (gets the video + caption ready in the TikTok tab)", self.tiktok_tab.make_one),
                     ("-", None),
                     ("ALL platforms", lambda: go("all", "ALL platforms"))])

    def make_menu(self):
        self._popup([("✨ Custom: upload + describe...", lambda: views.custom_window(self)),
                     ("-", None),
                     ("A clip (Short/TikTok)", lambda: self.run(["machine.py", "produce"], title="Make a clip")),
                     ("A meme", lambda: self.run(["machine.py", "test-picture", "meme"], title="Make a meme")),
                     ("A carousel", lambda: self.run(["machine.py", "test-picture", "carousel"], title="Make a carousel")),
                     ("An AI image", lambda: self.run(["machine.py", "test-picture", "ai_image"], title="Make an AI image")),
                     ("An AI music track", lambda: self.run(["machine.py", "make-music", "hype"], title="Make an AI music track"))])

    def fetch(self):
        n = simpledialog.askinteger("Get more videos", "How many of your videos/streams to pull in now?\n"
                                    "(streams take a while to download and analyze)", initialvalue=3, minvalue=1, maxvalue=10)
        if n:
            self.run(["machine.py", "fetch", str(n)], title=f"Get {n} more videos")

    def settings_menu(self):
        def gpu(mode):
            self.run(["machine.py", "gpu", mode], title=f"GPU mode: {mode}")

        def notepad(*files):
            for f in files:
                compat.edit_file(f, HERE)

        def delete_post():
            self.run(["threadsbot.py", "report"], title="Threads posts (find the number to delete)")
            n = simpledialog.askinteger("Delete a Threads post", "Post number to delete (see the list in Output):")
            if n and messagebox.askyesno("Delete", f"Delete Threads post #{n} for good?"):
                self.after(1500, lambda: self.run(["threadsbot.py", "delete", str(n)], live=True, stdin_text="y\n",
                                                  title=f"Delete Threads post {n}"))

        def set_token(var, label):
            t = simpledialog.askstring(label, f"Paste the new {label} (it's saved privately on this computer):",
                                       show="*", parent=self)
            if t and t.strip():
                r = compat.set_secret(var, t.strip())
                os.environ[var] = t.strip()  # this app + everything it starts uses it right away
                messagebox.showinfo(label, "Saved. The autopilot uses it from its next run." if r.returncode == 0
                                    else "Couldn't save it to secrets.env.")
                self.refresh(once=True)

        self._popup([
            ("⚙  Edit all settings", lambda: views.settings_window(self)),
            ("📺  Recurring series", lambda: views.series_window(self)),
            ("🎭  Edit the bot's personality", lambda: notepad("persona.md")),
            ("✍  Edit the example posts it learns its style from", lambda: notepad("examples.md")),
            ("-", None),
            ("Accounts", [
                ("🔑  Paste a new Threads token", lambda: set_token("THREADS_TOKEN", "Threads token")),
                ("Connect YouTube Shorts channel", lambda: self.run(["machine.py", "setup-youtube"], title="Connect YouTube")),
                ("Connect TikTok", lambda: self.run(["machine.py", "setup-tiktok"], title="Connect TikTok")),
                ("Connect Instagram", lambda: self.run(["machine.py", "setup-instagram"], title="Connect Instagram")),
                ("Facebook: check the Page is ready for Reels", lambda: self.run(["machine.py", "fb-check"], title="Facebook check")),
            ]),
            ("Graphics card (GPU)", [
                ("Game detection ON: pause while I'm gaming", lambda: gpu("auto")),
                ("Game detection OFF: always run, never check", lambda: gpu("on")),
                ("Never use the GPU", lambda: gpu("off")),
                ("Games that should NOT pause it (light_games.txt)", lambda: notepad("light_games.txt")),
            ]),
            ("Checks & fixes", [
                ("Threads self-check", lambda: self.run(["threadsbot.py", "check"], title="Threads self-check")),
                ("Instagram DMs: check they work", lambda: self.run(["machine.py", "dm-check"], title="Instagram DM check")),
                ("Instagram DMs: answer now", lambda: self.run(["machine.py", "dm-now"], live=True, title="Answer Instagram DMs")),
                ("Instagram: check stats + reply to comments now", lambda: self.run(["machine.py", "ig-check"], live=True,
                                                                                    title="Instagram check")),
                ("TikTok: what happened to each video", lambda: self.run(["machine.py", "tiktok-status"], title="TikTok status")),
                ("Phone alerts: show my ntfy topic + send a test", lambda: self.run(["machine.py", "ntfy-test"],
                                                                                    title="Phone alert test")),
                ("Delete a Threads post", delete_post),
                ("Install full FFmpeg (only if captions are missing)",
                 lambda: compat.install_ffmpeg()),
            ]),
            ("Voice & music", [
                ("Voice clone: say something in my voice", self.voice_test),
                ("Voice clone: install (one time, ~4 GB)",
                 lambda: compat.install_voice(HERE)),
                ("AI music: make a test track", lambda: self.run(["machine.py", "make-music", "hype"],
                                                                title="Making an AI music track")),
                ("AI music: install (one time, 9.3 GB)", lambda: self.run(["machine.py", "install-music"],
                                                                         title="Installing AI music (ACE-Step 1.5)")),
            ]),
            ("The bot's brain files", [
                ("Memory (lessons)", lambda: notepad("memory.md")),
                ("Channel knowledge", lambda: notepad("channel.md")),
                ("Keyword rules", lambda: notepad("keywords.md")),
                ("Raw settings file", lambda: notepad("machine_config.json")),
            ]),
            ("-", None),
            ("Start with Windows: turn " + ("OFF" if autostart_wanted() else "ON"), self.toggle_autostart),
            ("Autopilot: turn ON / fix the schedule", lambda: self.run_ps("autopilot-setup.ps1", "Fix / restart autopilot schedule")),
            ("Autopilot: turn OFF", self._autopilot_off),
        ])

    def _autopilot_off(self):
        if messagebox.askyesno("Autopilot", "Turn the autopilot OFF?\n\nNothing posts or replies on its own until you "
                                            "turn it back on."):
            compat.autopilot_off()
            self.refresh(once=True)

    def _mode_moved(self):
        i = int(self.mode_var.get())
        self.mode_label.config(text=["My stuff  (your channel, lore, clips)", "Mix  (both, the AI learns which works)",
                                     "Trending  (popular stuff, none of mine)"][i])

    def _mode_save(self):
        key = self.mode_keys[int(self.mode_var.get())]
        try:
            cfg = json.loads(read("machine_config.json") or "{}")
        except Exception:
            return
        if not cfg or cfg.get("content_mode") == key:
            return
        cfg["content_mode"] = key
        with open("machine_config.json.tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace("machine_config.json.tmp", "machine_config.json")
        self.status.config(text=f"Content mode: {self.mode_names[self.mode_keys.index(key)]}. Used from the next "
                                "AI video / post.", fg=GOOD)

    def toggle_autostart(self):
        try:
            cfg = json.loads(read("machine_config.json") or "{}")
        except Exception:
            cfg = {}
        if not cfg:
            return messagebox.showerror("Start with Windows", "Couldn't read the settings file.")
        cfg["start_with_windows"] = not cfg.get("start_with_windows", True)
        with open("machine_config.json.tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace("machine_config.json.tmp", "machine_config.json")
        ok = set_autostart(cfg["start_with_windows"])
        messagebox.showinfo("Start with Windows", ("Clip Machine now opens (minimized) when Windows starts."
                                                   if cfg["start_with_windows"] else "It no longer starts with Windows.")
                            if ok else "Couldn't change the Windows startup entry.")

    def toggle_autopilot(self):
        """Same as autopilot-ON.bat (turn on) / removing the scheduled tasks (turn off), from inside the app."""
        if self._autopilot_on:
            self._autopilot_off()
        else:
            self.run_ps("autopilot-setup.ps1", "Turning the autopilot ON")

    def voice_test(self):
        line = simpledialog.askstring("AI voice test", "What should your AI voice say?",
                                      initialvalue=profile.get("sample_voice_line"))
        if line:
            self.run(["machine.py", "voice-test"] + line.split(), title="AI voice test")

    def run_ps(self, script, title):
        if self.busy():
            messagebox.showinfo("Busy", "Something is still running.")
            return
        self.tabs.select(0)
        self._label = title
        self.out._shown = None
        set_text(self.out, f"▶ {title}\n\n")
        self._started = time.time()

        def work():
            try:
                p = subprocess.run(compat.script_cmd(script, HERE), capture_output=True, text=True, creationflags=NOWIN)
                self.q.put(("out", (p.stdout or "") + (p.stderr or "")))
                self.q.put(("done", p.returncode))
            except Exception as e:
                self.q.put(("out", f"[couldn't run it: {e}]\n"))
                self.q.put(("done", 1))
        threading.Thread(target=work, daemon=True).start()

    # ---------- chat ----------
    def open_chat(self):
        self.tabs.select(self.chat_tab)
        self.chat_in.focus_set()
        if self.chat_proc and self.chat_proc.poll() is None:
            return
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.configure(state="disabled")
        self._append(self.chat_box, "Starting the bot (the first answer takes a moment while the AI loads)...\n\n", "muted")
        try:
            self.chat_proc = subprocess.Popen([PY, "threadsbot.py", "chat"], cwd=HERE, env=env(False), stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                              encoding="utf-8", errors="replace", creationflags=NOWIN)
        except Exception as e:
            return self._append(self.chat_box, f"[couldn't start the chat: {e}]\n", "muted")
        proc = self.chat_proc

        def reader():
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                buf += ch
                if ch == "\n" or buf.endswith("You: "):
                    if buf.strip() and buf.strip() != "You:":
                        self.q.put(("chat", buf if ch == "\n" else ""))
                    buf = ""
            self.q.put(("chat", "\n[chat closed: open the Chat tab again to restart it]\n"))
        threading.Thread(target=reader, daemon=True).start()

    def send_chat(self):
        msg = self.chat_in.get().strip()
        if not msg:
            return
        if not (self.chat_proc and self.chat_proc.poll() is None):
            self.open_chat()
        self.chat_in.delete(0, "end")
        self._append(self.chat_box, f"You: {msg}\n", "you")
        try:
            self.chat_proc.stdin.write(msg + "\n")
            self.chat_proc.stdin.flush()
        except Exception:
            self._append(self.chat_box, "[the chat isn't running; open the Chat tab again]\n", "muted")

    def on_close(self):
        self.tiktok_tab.stop_preview()
        if self.chat_proc and self.chat_proc.poll() is None:
            try:  # let the bot save what it learned from the chat
                self.chat_proc.stdin.write("quit\n")
                self.chat_proc.stdin.flush()
                self.chat_proc.wait(timeout=20)
            except Exception:
                try:
                    self.chat_proc.kill()
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
