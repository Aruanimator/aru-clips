"""Audience signals: live chat (YouTube + Twitch), chat replays, YouTube "Most replayed", and loudness,
fused into one hype score per second of video."""
import json, re, socket, ssl, sys, time, threading, pathlib, random, subprocess
import numpy as np
import requests
import config
from config import popen, load_settings, FFMPEG

# ------------------------------------------------------------------ chat message scoring
HYPE_RE = re.compile(
    r"\b(kekw|omegalul|lul+|lmf?ao+|xd+|pog+(?:gers|champ)?|pogu|ez+|monka\w*|pepe\w*|sadge|copium|aware|hold ?up|"
    r"no ?way|wtf|holy|bro+|nah+|yo+o|insane|crazy|goat|w+|l+|ha(?:ha)+)\b|\?{3,}|!{3,}|[😂🤣💀😭🔥😱🤯😳]", re.I)
CLIP_RE = re.compile(r"\b(clip+ (it|that|this)|clipped|\+?clip\b|clip!)", re.I)
# messages that say nothing about the stream: bots, ads, raids, links (idea from gtavi-claudecode, MIT)
NOISE_RE = re.compile(r"giveaway|\braid(ing|ed)?\b|\bbrb\b|be right back|ad break|sponsor|subscribe|discord\.gg|https?://|"
                      r"promo code|follow me|\bsub(scribed)? (for|with)|gifted|!\w+$|raiders?\b|hype ?train|\bcheer\d+|\bbits\b|"
                      r"welcome (in|to)|first time chat", re.I)
STOPWORDS = set("the a an and or to of is it in i you that this on for with was are be so but not my me we he she they "
                "what just like at as if do get its im".split())


def is_noise(text):
    return not text.strip() or bool(NOISE_RE.search(text))


def msg_weight(text):
    t = text.strip()
    if is_noise(t):
        return 0.0
    w = 1.0
    hits = len(HYPE_RE.findall(t))
    w += min(3, hits) * 0.6
    if CLIP_RE.search(t):
        w += 4  # viewers asking for a clip is the strongest sign
    if len(t) >= 6 and t.upper() == t and re.search(r"[A-Z]", t):
        w += 0.4  # all caps
    return w


# ------------------------------------------------------------------ YouTube chat (replay file and live)
def _yt_text(renderer):
    out = []
    for r in (renderer.get("message") or {}).get("runs", []):
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            sc = (e.get("shortcuts") or [None])[0]
            out.append(e.get("emojiId") if e.get("emojiId") and len(e.get("emojiId")) <= 2 else (sc or ""))
    return "".join(out)


def parse_yt_chat_lines(lines, live=False):
    """Returns [(seconds, text)]. Replays use the video offset; live chat uses the wall clock (seconds since epoch)."""
    out = []
    for line in lines:
        try:
            j = json.loads(line)
        except Exception:
            continue
        rep = j.get("replayChatItemAction") or {}
        off = rep.get("videoOffsetTimeMsec") or j.get("videoOffsetTimeMsec")
        for a in rep.get("actions", []):
            item = (a.get("addChatItemAction") or {}).get("item") or {}
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            if live:
                ts = r.get("timestampUsec")
                if ts:
                    out.append((int(ts) / 1e6, _yt_text(r)))
            elif off is not None:
                out.append((int(off) / 1000, _yt_text(r)))
    return out


def yt_replay_chat(job_dir):
    p = next(pathlib.Path(job_dir).glob("chat*.json"), None)
    if not p:
        return []
    return parse_yt_chat_lines(p.read_text("utf-8", "ignore").splitlines())


# ------------------------------------------------------------------ Twitch VOD chat replay
TW_GQL = "https://gql.twitch.tv/gql"
TW_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's public web client id
TW_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def twitch_video_id(url):
    m = re.search(r"twitch\.tv/(?:\w+/)?videos?/(\d+)", url) or re.search(r"[?&]video=v?(\d+)", url)
    return m.group(1) if m else None


def twitch_channel(url):
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]{3,25})/?(?:$|\?)", url)
    if m and m.group(1).lower() not in ("videos", "directory", "settings", "p", "search"):
        return m.group(1).lower()
    return None


def _tw_page(vid, offset):
    body = [{"operationName": "VideoCommentsByOffsetOrCursor",
             "variables": {"videoID": vid, "contentOffsetSeconds": int(offset)},
             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TW_HASH}}}]
    r = requests.post(TW_GQL, json=body, headers={"Client-Id": TW_CLIENT}, timeout=20)
    r.raise_for_status()
    data = r.json()[0].get("data") or {}
    edges = ((((data.get("video") or {}).get("comments")) or {}).get("edges")) or []
    out = []
    for e in edges:
        n = e.get("node") or {}
        txt = "".join(f.get("text", "") for f in ((n.get("message") or {}).get("fragments") or []))
        out.append((float(n.get("contentOffsetSeconds", 0)), txt))
    return out


def twitch_replay_chat(url, duration, log=print, budget_s=420, lanes=8):
    """Reads a Twitch VOD's chat by walking it in parallel lanes (offset paging, no login needed)."""
    vid = twitch_video_id(url)
    if not vid or not duration:
        return []
    msgs, seen, lock = [], set(), threading.Lock()
    t_end = time.time() + budget_s
    span = duration / lanes
    errors = [0]

    def lane(k):
        off, stop = k * span, min(duration, (k + 1) * span)
        while off < stop and time.time() < t_end:
            try:
                page = _tw_page(vid, off)
            except Exception:
                errors[0] += 1
                if errors[0] > 20:
                    return
                time.sleep(1); continue
            if not page:
                off += 30; continue
            with lock:
                for m in page:
                    key = (round(m[0], 2), m[1])
                    if key not in seen and m[0] < stop + 1:
                        seen.add(key); msgs.append(m)
            last = max(m[0] for m in page)
            off = max(off + 1, last + 0.5)
    ts = [threading.Thread(target=lane, args=(k,), daemon=True) for k in range(lanes)]
    for t in ts: t.start()
    for t in ts: t.join()
    msgs.sort()
    if msgs:
        log(f"Read {len(msgs):,} Twitch chat messages" + (" (time limit reached, partial chat)" if time.time() >= t_end else ""))
    elif errors[0]:
        log("Twitch chat replay isn't available for this video")
    return msgs


# ------------------------------------------------------------------ live chat readers (wall-clock timestamps)
class TwitchLiveChat(threading.Thread):
    """Anonymous read-only Twitch IRC connection."""

    def __init__(self, channel):
        super().__init__(daemon=True)
        self.channel, self.msgs, self.stop = channel.lower(), [], threading.Event()

    def run(self):
        use_tls = True
        while not self.stop.is_set():
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697 if use_tls else 6667), timeout=30)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv") if use_tls else raw
                nick = f"justinfan{random.randint(10000, 99999)}"
                s.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
                s.settimeout(60)
                buf = b""
                while not self.stop.is_set():
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        s.sendall(b"PING :tmi.twitch.tv\r\n"); continue
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\r\n")
                    now = time.time()
                    for ln in lines:
                        t = ln.decode("utf-8", "ignore")
                        if t.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG #" in t:
                            self.msgs.append((now, t.split(" :", 1)[1] if " :" in t else ""))
                s.close()
            except ssl.SSLError:
                use_tls = False  # some networks block TLS chat; plain IRC is read-only anyway
                time.sleep(2)
            except Exception:
                time.sleep(5)


class YouTubeLiveChat(threading.Thread):
    """Follows a YouTube live chat with yt-dlp and reads its growing file."""

    def __init__(self, url, folder):
        super().__init__(daemon=True)
        self.url, self.folder, self.msgs, self.stop = url, pathlib.Path(folder), [], threading.Event()
        self.proc = None

    def run(self):
        s = load_settings()
        cmd = [config.PYEXE, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
               "-o", str(self.folder / "livechat"), self.url]
        if s.get("cookies_browser"):
            cmd[4:4] = ["--cookies-from-browser", s["cookies_browser"]]
        self.proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started, pos = time.time(), {}
        while not self.stop.is_set():
            for p in self.folder.glob("livechat*.json*"):
                try:
                    with open(p, "rb") as f:
                        f.seek(pos.get(p.name, 0))
                        data = f.read()
                except Exception:
                    continue
                if not data:
                    continue
                cut = data.rfind(b"\n") + 1
                pos[p.name] = pos.get(p.name, 0) + cut
                self.msgs += parse_yt_chat_lines(data[:cut].decode("utf-8", "ignore").splitlines(), live=True)
            if self.proc.poll() is not None and time.time() - started > 15 and not self.stop.is_set():
                self.proc = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # chat connection dropped
                started = time.time()
            time.sleep(2)
        try:
            self.proc.terminate()
        except Exception:
            pass


def start_live_chat(url, folder, log=print):
    if "twitch.tv" in url:
        ch = twitch_channel(url)
        if ch:
            c = TwitchLiveChat(ch); c.start(); log(f"Reading Twitch chat for #{ch}"); return c
    elif "youtu" in url:
        c = YouTubeLiveChat(url, folder); c.start(); log("Reading YouTube live chat"); return c
    return None


# ------------------------------------------------------------------ fusion
def heatmap_array(heat, total):
    a = np.zeros(int(total) + 2)
    for h in heat or []:
        s, e, v = int(h.get("start_time", 0)), int(h.get("end_time", 0)) + 1, float(h.get("value", 0))
        a[max(0, s):max(0, min(len(a), e))] = v
    return a


def timeline(total, chat=None, heat=None, loud=None, lag=6.0, profile="general", baseline=None):
    """Hype score per second plus parts used to explain it. chat: [(sec, text)] on the video's clock."""
    n = int(total) + 2
    parts = {}
    score = np.zeros(n)
    if chat:
        w = np.zeros(n); cnt = np.zeros(n); clips = np.zeros(n)
        for t, txt in chat:
            i = int(t - lag)  # chat reacts a few seconds after the moment
            mw = msg_weight(txt)
            if 0 <= i < n and mw:
                w[i] += mw; cnt[i] += 1
                if CLIP_RE.search(txt): clips[i] += 1
        k = np.ones(8) / 8
        rate = np.convolve(w, k, "same")
        base = baseline if baseline else max(0.05, float(np.median(rate[rate > 0])) if np.any(rate > 0) else 0.05)
        ratio = rate / base
        parts["chat_ratio"] = ratio
        parts["clip_calls"] = np.convolve(clips, np.ones(15), "same")
        score += np.clip(np.log2(np.maximum(ratio, 1e-3)), -1, 4) * 1.0 + np.minimum(parts["clip_calls"], 5) * 0.5
        parts["chat_base"] = base
    if heat:
        h = heatmap_array(heat, total)[:n]
        h = np.pad(h, (0, n - len(h)))
        parts["heat"] = h
        hz = (h - np.median(h)) / (np.std(h) + 1e-6)
        score += np.clip(hz, -1, 4) * 1.2
    if loud is not None and len(loud) > 10:
        L = np.pad(loud[:n], (0, max(0, n - len(loud))), constant_values=float(np.median(loud)))
        # jumps above the level of the last ~30 s, so constant game noise doesn't count as hype
        local = np.convolve(np.pad(L, 15, mode="edge"), np.ones(31) / 31, "valid")[:n]
        rel = L - local
        z = (rel - np.median(rel)) / (np.std(rel) + 1e-6)
        z = np.convolve(z, np.ones(3) / 3, "same")
        parts["loud"] = z
        score += np.clip(z, -1, 4) * (0.25 if profile == "gaming" else 0.4)
    return score, parts


def peaks(score, k, min_gap):
    s = score.copy()
    out = []
    for _ in range(k):
        i = int(np.argmax(s))
        if s[i] <= 0.3:
            break
        out.append((i, float(score[i])))
        s[max(0, i - int(min_gap)):i + int(min_gap)] = -9
    return out


def describe(parts, t):
    bits = []
    i = int(t)
    if "chat_ratio" in parts:
        r = float(np.max(parts["chat_ratio"][max(0, i - 6):i + 8]))
        if r >= 1.6:
            bits.append(f"chat {r:.1f}x busier than normal")
        c = float(np.max(parts["clip_calls"][max(0, i - 6):i + 8]))
        if c >= 1:
            bits.append(f"{int(c)} viewers typed \"clip it\"")
    if "heat" in parts and len(parts["heat"]) > i:
        v = float(np.max(parts["heat"][max(0, i - 5):i + 6]))
        if v >= 0.6:
            bits.append("one of the most replayed parts on YouTube" if v >= 0.9 else f"{int(v * 100)}% on YouTube's most-replayed graph")
    if "loud" in parts:
        z = float(np.max(parts["loud"][max(0, i - 4):i + 5]))
        if z > 1.8:
            bits.append("audio spike")
    return ", ".join(bits)


def chat_sample(chat, t0, t1, lag=6.0, k=14):
    """Most typical messages around a moment (raw chat times, so add the lag)."""
    win = [txt.strip() for t, txt in chat if t0 + lag - 2 <= t <= t1 + lag + 6 and not is_noise(txt)]
    if not win:
        return []
    counts = {}
    for m in win:
        key = m.lower()[:60]
        counts[key] = counts.get(key, [0, m])
        counts[key][0] += 1
    ranked = sorted(counts.values(), key=lambda x: (-x[0], -msg_weight(x[1])))
    return [f"{m} (x{c})" if c > 1 else m for c, m in ranked[:k]]


# ------------------------------------------------------------------ instant spike detection (live)
def spike_now(msgs, now, window=10, baseline=300, ratio=3.0, min_msgs=8):
    """Checks the last `window` seconds of live chat (wall-clock times) against the stream's recent normal pace.
    Returns (msgs_in_window, times_normal) when chat just exploded, else None."""
    e = now - 3  # let chat batches land first
    recent = [(t, x) for t, x in msgs if e - window < t <= e and not is_noise(x)]
    if len(recent) < min_msgs:
        return None
    w_now = sum(msg_weight(x) for _, x in recent) / window
    span = min(baseline, max(0.0, (e - window) - (msgs[0][0] if msgs else e)))
    if span < 30:
        return None
    base = sum(msg_weight(x) for t, x in msgs if e - window - span < t <= e - window) / span
    r = w_now / max(base, 0.02)
    return (len(recent), r) if r >= ratio else None


def top_words(msgs, t0, t1, n=3):
    cnt = {}
    for t, x in msgs:
        if t0 <= t <= t1 and not is_noise(x):
            for wd in re.findall(r"[\w']{2,}|[😂🤣💀😭🔥]", x.lower()):
                if wd not in STOPWORDS:
                    cnt[wd] = cnt.get(wd, 0) + 1
    return [w for w, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:n]]


# ------------------------------------------------------------------ live stream finder
def twitch_top_streams(game="Grand Theft Auto VI", limit=20):
    q = ("query($name:String!,$n:Int!){ game(name:$name){ streams(first:$n){ edges{ node{ title viewersCount createdAt "
         "previewImageURL(width:320,height:180) broadcaster{ login displayName } } } } } }")
    r = requests.post(TW_GQL, json={"query": q, "variables": {"name": game, "n": int(limit)}},
                      headers={"Client-Id": TW_CLIENT}, timeout=15)
    r.raise_for_status()
    g = ((r.json().get("data") or {}).get("game")) or {}
    out = []
    for e in ((g.get("streams") or {}).get("edges") or []):
        nd = e.get("node") or {}
        b = nd.get("broadcaster") or {}
        if b.get("login"):
            out.append({"platform": "Twitch", "channel": b.get("displayName") or b["login"], "title": nd.get("title") or "",
                        "viewers": nd.get("viewersCount") or 0, "url": f"https://www.twitch.tv/{b['login']}",
                        "thumb": nd.get("previewImageURL") or "", "started": nd.get("createdAt") or ""})
    return sorted(out, key=lambda x: -x["viewers"])


def youtube_live_search(query, limit=20):
    import yt_dlp
    from urllib.parse import quote_plus
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgJAAQ%253D%253D"  # "Live" filter
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": limit}) as y:
        info = y.extract_info(url, download=False)
    out = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        if e.get("live_status") not in (None, "is_live"):
            continue
        out.append({"platform": "YouTube", "channel": e.get("channel") or e.get("uploader") or "", "title": e.get("title") or "",
                    "viewers": e.get("concurrent_view_count") or e.get("view_count") or 0,
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                    "thumb": ((e.get("thumbnails") or [{}])[-1]).get("url", ""), "started": ""})
    return sorted(out, key=lambda x: -(x["viewers"] or 0))


def find_live_streams(query="gta 6", game="Grand Theft Auto VI", limit=15):
    from concurrent.futures import ThreadPoolExecutor
    res, errs = {}, []
    def tw():
        try: res["tw"] = twitch_top_streams(game, limit)
        except Exception: errs.append("Couldn't reach Twitch. Check your internet connection."); res["tw"] = []
    def yt():
        try: res["yt"] = youtube_live_search(query, limit)
        except Exception: errs.append("Couldn't search YouTube right now."); res["yt"] = []
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda f: f(), (tw, yt)))
    return {"twitch": res.get("tw", []), "youtube": res.get("yt", []), "errors": errs}
