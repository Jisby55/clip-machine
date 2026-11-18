"""
GPU guard: heavy work (rendering, transcription, AI images/video, voice) only runs when allowed.

machine_config.json "gpu_mode":
  auto -> allowed unless a game is running (games in light_games.txt don't count)
  on   -> always allowed
  off  -> never
"""
import os
import subprocess
import sys
import time

import mm_safety

GAME_HINTS = ("steamapps", "epic games", "riot games", "battle.net", "xboxgames", "gog galaxy\\games",
              "ubisoft game launcher\\games", "ea games")
NOT_GAMES = {"ollama.exe", "ollama app.exe", "python.exe", "pythonw.exe", "ffmpeg.exe", "obs64.exe",
             "comfyui.exe", "comfy desktop.exe", "chrome.exe", "zen.exe", "discord.exe", "steam.exe",
             "steamwebhelper.exe", "explorer.exe", "dwm.exe", "nvidia app.exe", "nvidia overlay.exe",
             "wallpaper32.exe", "wallpaper64.exe", "wallpaperui.exe", "epicgameslauncher.exe"}


# Launcher/background helpers that live in game folders but aren't games
LAUNCHER_BITS = ("helper", "overlay", "launcher", "service", "crash", "eos", "epiconline", "webhelper",
                 "update", "installer", "anticheat", "easyanticheat", "battleye", "redist", "bootstrap", "agent",
                 "bugsplat", "crashpad", "crashhandler", "reporter", "monitor")


def light_games():
    if not os.path.exists("light_games.txt"):
        return set()
    with open("light_games.txt", encoding="utf-8") as f:
        return {l.strip().lower() for l in f if l.strip() and not l.startswith("#")}


def running_games():
    """Processes that look like games: launched from a game library folder, or using lots of GPU."""
    if sys.platform != "win32":
        return set()  # Mac = always-on server, nothing to pause for
    found = set()
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            name = (p.info.get("name") or "").lower()
            exe = (p.info.get("exe") or "").lower()
            if name in NOT_GAMES or any(w in name for w in LAUNCHER_BITS):
                continue
            if any(h in exe for h in GAME_HINTS):
                found.add(name)
    except Exception:
        pass
    # Anything else hammering the GPU through the NVIDIA driver counts as a game too
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=process_name,used_memory",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            parts = [x.strip() for x in line.rsplit(",", 1)]
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 1500:
                name = os.path.basename(parts[0]).lower()
                if name not in NOT_GAMES:
                    found.add(name)
    except Exception:
        pass
    found = found - light_games()
    if found:
        _note_game()
    return found


LAST_GAME = "last_game.txt"  # when a game was last seen running (checked by the app every 30 s and by each machine run)


def _note_game():
    try:
        with open(LAST_GAME, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def quiet_minutes():
    """Minutes since a game was last seen running."""
    if sys.platform != "win32":
        return 1e6
    try:
        with open(LAST_GAME, encoding="utf-8") as f:
            return max(0.0, (time.time() - float(f.read().strip())) / 60)
    except (OSError, ValueError):
        _note_game()  # first time: start the clock now, so heavy AI waits a full quiet period before it trusts it
        return 0.0


def heavy_ok():
    """(ok, reason) for the HEAVY stuff that can freeze the PC: AI video, AI images, AI music (ComfyUI).
    Automatic runs only do it when no game has been running for `ai_quiet_minutes` (default 90).
    Things you start yourself from the app always run."""
    if os.environ.get("BOT_INTERACTIVE") == "1":
        return True, "you started it"
    ok, why = gpu_allowed()
    if not ok:
        return False, why
    if mm_safety.config().get("gpu_mode", "auto").lower() == "on":
        return True, why
    need = float(mm_safety.config().get("ai_quiet_minutes", 90))
    quiet = quiet_minutes()
    if quiet < need:
        return False, f"a game was running {quiet:.0f} min ago; heavy AI waits until {need:.0f} min with no games"
    return True, f"no games for {quiet:.0f} min"


def free_gpu_now():
    """Unload the BOT's own AI model from Ollama if it's still loaded (frees the VRAM for your game). Other models
    are left alone: they belong to something else you're running (like the Dwarf Fortress AI mod)."""
    import requests
    if override_active():
        return  # a job you OK'd is using the model right now: don't pull it out from under it
    mine = {str(m).lower() for m in (mm_safety.config().get("ollama_model"), os.environ.get("BOT_MODEL"),
                                     "qwen3:8b", "nomic-embed-text", "nomic-embed-text:latest") if m}
    try:
        for m in requests.get("http://localhost:11434/api/ps", timeout=5).json().get("models", []):
            if m.get("name", "").lower() not in mine:
                continue
            requests.post("http://localhost:11434/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=20)
            print(f"Freed the GPU: unloaded {m['name']}")
    except Exception:
        pass


def game_pause():
    """(paused, reason). Background runs stop completely while you play; things YOU start from the app still run."""
    if os.environ.get("BOT_INTERACTIVE") == "1":
        return False, ""
    ok, why = gpu_allowed()
    return (not ok and "game" in why), why


OVERRIDE = "gpu_override.txt"  # exists while a job you OK'd mid-game is running (the app removes it when it finishes)


def override_active():
    """You pressed 'run anyway' in the app: the GPU is allowed until that job finishes (2 h safety limit)."""
    if os.environ.get("BOT_FORCE_GPU") == "1":
        return True
    try:
        return time.time() - os.path.getmtime(OVERRIDE) < 2 * 3600
    except OSError:
        return False


def gpu_allowed():
    mode = mm_safety.config().get("gpu_mode", "auto").lower()
    if os.environ.get("BOT_FORCE_GPU") == "1":
        return True, "you said run it anyway"
    if mode == "on":
        return True, "gpu_mode=on"
    if mode == "off":
        return False, "gpu_mode=off"
    games = running_games()
    if games:
        return False, "game running: " + ", ".join(sorted(games))
    return True, "no game running"
