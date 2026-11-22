"""Paths, settings and small shared helpers."""
import json, os, re, shutil, sys, threading, pathlib, subprocess

APP = pathlib.Path(__file__).resolve().parent
ASSETS = APP / "assets"
HOME = pathlib.Path.home()
DATA = pathlib.Path(os.environ.get("ARU_DATA") or (HOME / "AruClips"))
UPLOADS = DATA / "uploads"
USER_FONTS = DATA / "fonts"
USER_SFX = DATA / "sfx"
FILLERS = DATA / "fillers"
CACHE = DATA / "cache"
for d in (DATA, UPLOADS, USER_FONTS, USER_SFX, FILLERS, CACHE):
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA / "settings.json"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: keep ffmpeg consoles hidden

DEFAULTS = {
    "engine": "auto",
    "groq_api_key": "",
    "groq_llm_model": "",
    "groq_whisper_model": "whisper-large-v3-turbo",
    "ollama_model": "qwen2.5:7b",
    "ollama_url": "http://localhost:11434",
    "local_whisper_model": "small",
    "output_dir": str(HOME / "Videos" / "AruClips"),
    "cookies_browser": "",
    "groq_extra_keys": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "backup_ai": "",
    "backup_key": "",
    "backup_model": "",
    "video_encoder": "auto",
    "num_clips": 6,
    "crop_mode": "face",
    "caption_style": "bold",
    "look": {},
}

_lock = threading.Lock()


SECRET_KEYS = ("groq_api_key", "groq_extra_keys", "gemini_api_key", "backup_key")


def load_settings():
    import secure
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
    except Exception:
        pass
    for k in SECRET_KEYS:  # stored encrypted, used as plain text
        if k in s:
            s[k] = secure.unprotect(s[k])
    return s


def save_settings(patch):
    import secure
    with _lock:
        s = load_settings()
        s.update(patch)
        disk = dict(s)
        for k in SECRET_KEYS:
            if disk.get(k):
                disk[k] = secure.protect(disk[k])
        SETTINGS_FILE.write_text(json.dumps(disk, indent=2), "utf-8")
        return s


def output_dir():
    p = pathlib.Path(load_settings()["output_dir"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _find_tool(name):
    exe = name + (".exe" if IS_WIN else "")
    for c in (APP.parent / "tools" / "ffmpeg" / "bin" / exe, APP.parent / "tools" / exe):
        if c.exists():
            return str(c)
    found = shutil.which(name)
    if found or not IS_WIN:
        return found
    # installed with winget in this session: not on PATH until Windows restarts the shell
    la = pathlib.Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local")) / "Microsoft" / "WinGet"
    for c in [la / "Links" / exe] + sorted((la / "Packages").glob(f"Gyan.FFmpeg*/*/bin/{exe}")):
        if c.exists():
            return str(c)
    return None


FFMPEG = _find_tool("ffmpeg")
# Python for helper processes: the windowless pythonw has no console streams, so use python.exe next to it
PYEXE = sys.executable
if IS_WIN and pathlib.Path(sys.executable).name.lower() == "pythonw.exe" and (pathlib.Path(sys.executable).parent / "python.exe").exists():
    PYEXE = str(pathlib.Path(sys.executable).parent / "python.exe")
FFPROBE = _find_tool("ffprobe")


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, creationflags=NO_WINDOW, **kw)


def popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=NO_WINDOW, **kw)


def slug(s, n=48):
    s = re.sub(r"[^\w\s-]", "", s or "", flags=re.U).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return (s[:n].strip("-") or "video")


def esc_json(o):
    return json.dumps(o, ensure_ascii=False, indent=1)
