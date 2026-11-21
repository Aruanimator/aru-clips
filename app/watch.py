"""Streamer watchlist: checks the streamers you clip every few minutes and starts live clipping when one goes live."""
import json, os, re, threading, time, uuid
from config import DATA
import media

FILE = DATA / "watchlist.json"
EVERY = 180  # seconds between checks
_lock = threading.RLock()
_thread = None
_status = {}  # id -> {"live": bool, "checked": t, "error": str}


def load():
    try:
        return json.loads(FILE.read_text("utf-8"))
    except Exception:
        return []


def _save(items):
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), "utf-8")
    os.replace(tmp, FILE)


def normalize(s):
    """Accepts a full link, a Twitch/Kick name, or a YouTube @handle."""
    s = s.strip()
    if re.match(r"https?://", s):
        if re.search(r"youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+)/?$", s):
            return s.rstrip("/") + "/live"
        return s
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}/live"
    m = re.match(r"(twitch|kick|yt|youtube):\s*(\S+)", s, re.I)
    if m:
        site, name = m.group(1).lower(), m.group(2)
        return {"twitch": f"https://www.twitch.tv/{name}", "kick": f"https://kick.com/{name}"}.get(
            site, f"https://www.youtube.com/@{name.lstrip('@')}/live")
    return f"https://www.twitch.tv/{s}"


def items():
    out = []
    for it in load():
        out.append(dict(it, **_status.get(it["id"], {})))
    return out


def add(target, opts, name=""):
    url = normalize(target)
    with _lock:
        lst = load()
        if any(x["url"] == url for x in lst):
            raise ValueError("This streamer is already on your list")
        it = {"id": uuid.uuid4().hex[:8], "url": url, "name": name or _name(url), "on": True, "opts": opts or {},
              "added": time.time(), "last_job": None, "armed": True}
        lst.append(it)
        _save(lst)
    return it


def _name(url):
    m = re.search(r"(?:twitch\.tv|kick\.com)/([^/?#]+)|youtube\.com/(@[^/?#]+)", url)
    return (m.group(1) or m.group(2)) if m else url


def edit(iid, **kw):
    with _lock:
        lst = load()
        for x in lst:
            if x["id"] == iid:
                x.update({k: v for k, v in kw.items() if k in ("on", "name", "opts")})
        _save(lst)


def remove(iid):
    with _lock:
        _save([x for x in load() if x["id"] != iid])
    _status.pop(iid, None)


def is_live(url):
    try:
        info = media.ydl_run(lambda y: y.extract_info(url, download=False, process=False), {"skip_download": True})
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next((e for e in info["entries"] if e), {})
        return bool(info.get("is_live") or info.get("live_status") == "is_live"), info.get("title") or ""
    except Exception:
        return False, ""  # offline channels usually answer with an error


def _loop(create, busy):
    """create(opts) -> job id; busy(job id) -> True while that job is still recording or paused."""
    while True:
        for it in load():
            if not it.get("on"):
                continue
            live, title = is_live(it["url"])
            _status[it["id"]] = {"live": live, "checked": time.time(), "title": title}
            if not live:
                if not it.get("armed"):
                    edit_armed(it["id"], True)  # went offline: start again the next time they go live
                continue
            if it.get("last_job") and busy(it["last_job"]):
                continue
            if not it.get("armed"):
                continue  # this broadcast already had a project (finished or stopped by you)
            opts = dict(it.get("opts") or {}, url=it["url"], source_type="live")
            jid = create(opts)
            with _lock:
                lst = load()
                for x in lst:
                    if x["id"] == it["id"]:
                        x.update(last_job=jid, armed=False, last_start=time.time())
                _save(lst)
        time.sleep(EVERY)


def edit_armed(iid, v):
    with _lock:
        lst = load()
        for x in lst:
            if x["id"] == iid:
                x["armed"] = v
        _save(lst)


def start(create, busy):
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, args=(create, busy), daemon=True)
    _thread.start()
