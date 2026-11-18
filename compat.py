"""Windows/Mac helpers for the Clip Machine. Everything platform-specific lives here so the rest of the code stays the same."""
import os
import subprocess
import sys
import types

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "secrets.env")
LABEL = "com.clipmachine.autopilot"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_env():
    """Keys live in secrets.env (KEY=value per line) next to the code, loaded at start-up (every OS)."""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if k.strip() and v:
                    os.environ.setdefault(k.strip(), v)
    except OSError:
        pass


def set_secret(var, value):
    """Save a key into secrets.env (every OS; Windows also sets it as a user variable)."""
    os.environ[var] = value
    if IS_WIN:  # also a permanent user variable, for scripts started outside the app
        subprocess.run(["setx", var, value], capture_output=True, creationflags=_NOWIN)
    try:
        lines = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, encoding="utf-8") as f:
                lines = [l.rstrip("\n") for l in f if not l.startswith(var + "=")]
        lines.append(f"{var}={value}")
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(ENV_FILE, 0o600)
        return types.SimpleNamespace(returncode=0)
    except OSError:
        return types.SimpleNamespace(returncode=1)


def open_file(path):
    if IS_WIN:
        os.startfile(path)  # noqa: B009
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def edit_file(name, cwd):
    if IS_WIN:
        subprocess.Popen(["notepad", name], cwd=cwd)
    elif IS_MAC:
        subprocess.Popen(["open", "-t", os.path.join(cwd, name)])
    else:
        subprocess.Popen(["xdg-open", os.path.join(cwd, name)])


def mono_font():
    return "Consolas" if IS_WIN else ("Menlo" if IS_MAC else "DejaVu Sans Mono")


def venv_python(env_dir):
    return os.path.join(env_dir, "Scripts", "python.exe") if IS_WIN else os.path.join(env_dir, "bin", "python")


def autopilot_status(task="ClipMachine Autopilot"):
    try:
        if IS_WIN:
            return subprocess.run(["schtasks", "/query", "/tn", task], capture_output=True,
                                  creationflags=_NOWIN).returncode == 0
        return subprocess.run(["launchctl", "list", LABEL], capture_output=True).returncode == 0
    except Exception:
        return None


def autopilot_off():
    if IS_WIN:
        for t in ("ClipMachine Autopilot", "ClipMachine Replies"):
            subprocess.run(["schtasks", "/delete", "/tn", t, "/f"], capture_output=True, creationflags=_NOWIN)
    else:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        try:
            os.remove(PLIST)
        except OSError:
            pass


def script_cmd(script, here):
    """Command that runs the 'turn autopilot on / fix it' script for this OS."""
    if IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(here, "windows", script)]
    return ["bash", os.path.join(here, "mac", "autopilot-setup.sh")]


def sleep_pc():
    if IS_WIN:
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          "Add-Type -AssemblyName System.Windows.Forms; "
                          "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"],
                         creationflags=_NOWIN)
    else:
        subprocess.Popen(["pmset", "sleepnow"])


def shutdown_pc():
    if IS_WIN:
        subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=_NOWIN)
    else:
        subprocess.Popen(["osascript", "-e", 'tell application "System Events" to shut down'])


def _terminal(cmd):
    subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{cmd}"'])


def install_ffmpeg():
    if IS_WIN:
        subprocess.Popen(["cmd", "/c", "start", "winget", "install", "-e", "--id", "Gyan.FFmpeg"])
    else:
        _terminal("brew install ffmpeg-full")


def install_voice(here):
    if IS_WIN:
        subprocess.Popen(["cmd", "/c", "start", "Install voice", os.path.join(here, "windows", "INSTALL-VOICE.bat")], cwd=here)
    else:
        _terminal(f"bash '{os.path.join(here, 'mac', 'install-voice.sh')}'")
