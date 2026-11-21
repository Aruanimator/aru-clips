"""Links, downloads and media probing (yt-dlp + ffmpeg)."""
import json, os, re, wave, pathlib, threading
import numpy as np
from config import FFMPEG, FFPROBE, run, load_settings

PLATFORMS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "tiktok": "TikTok", "instagram": "Instagram",
             "facebook": "Facebook", "twitter": "X", "x": "X", "vimeo": "Vimeo", "reddit": "Reddit", "dailymotion": "Dailymotion",
             "rumble": "Rumble", "soundcloud": "SoundCloud", "bilibili": "Bilibili", "linkedin": "LinkedIn", "snapchat": "Snapchat"}

LOGIN_HINT = ("This link needs you to be signed in (private, age-restricted, Facebook or Instagram). Open Settings and pick "
              "the browser you're signed in to under Browser login, then try again.")


def platform_of(url, extractor=""):
    ex = (extractor or "").lower()
    for k, v in PLATFORMS.items():
        if ex.startswith(k):
            return v
    host = re.sub(r"^https?://(www\.|m\.)?", "", url.lower()).split("/")[0]
    if "youtu" in host: return "YouTube"
    if host in ("x.com", "t.co"): return "X"
    if "fb.watch" in host: return "Facebook"
    labels = host.split(".")
    for k, v in PLATFORMS.items():
        if k in labels:
            return v
    return host.split(".")[-2].capitalize() if "." in host else ""


_no_cookies = {"on": False}  # set when the browser sign-in can't be read on this PC


def _ydl_opts(extra=None, cookies=True):
    s = load_settings()
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    if FFMPEG:
        o["ffmpeg_location"] = str(pathlib.Path(FFMPEG).parent)
    if os.name == "nt":
        o["compat_opts"] = ["no-certifi"]  # use Windows' own certificates (antivirus / company networks)
    if cookies and s.get("cookies_browser") and not _no_cookies["on"]:
        o["cookiesfrombrowser"] = (s["cookies_browser"],)
    if extra:
        o.update(extra)
    return o


def cookie_error(e):
    t = str(e).lower()
    return "cookie" in t and any(k in t for k in ("could not copy", "decrypt", "dpapi", "permission", "locked", "failed to", "unable"))


def ydl_run(fn, extra=None):
    """Runs a yt-dlp call; if the browser sign-in can't be read (Chrome/Edge on Windows often blocks it),
    tries again without it, which works for every public video and stream."""
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(extra)) as y:
            return fn(y)
    except Exception as e:
        if not (cookie_error(e) and load_settings().get("cookies_browser")):
            raise
        _no_cookies["on"] = True
        with yt_dlp.YoutubeDL(_ydl_opts(extra, cookies=False)) as y:
            return fn(y)


def friendly_error(e):
    t = str(e)
    low = t.lower()
    if any(k in low for k in ("sign in", "login", "log in", "cookies", "private", "age", "confirm your age", "authentication")):
        return LOGIN_HINT
    if "cookie" in low and any(k in low for k in ("could not copy", "decrypt", "dpapi")):
        return ("The app couldn't read your browser sign-in (Chrome and Edge on Windows often block this). "
                "Public videos work without it: in Settings set Browser login to None. For private videos, pick Firefox.")
    if "requested format is not available" in low:
        return ("The site didn't offer a recordable stream. Close the app, start it again (that updates the downloader) "
                "and retry. If it keeps happening, the stream may be members-only or not live yet.")
    if "unsupported url" in low:
        return "This site isn't supported. Download the video and drop the file here instead."
    t = re.sub(r"\x1b\[[0-9;]*m", "", t).replace("ERROR: ", "")
    return t[:300]


def kind_of(info, url):
    ls = info.get("live_status") or ""
    if info.get("is_live") or ls in ("is_live", "is_upcoming"):
        return "live"
    if ls in ("was_live", "post_live") or info.get("was_live") or "/videos/" in url and "twitch" in url:
        return "stream"
    if (info.get("duration") or 0) >= 3 * 3600:
        return "stream"
    return "video"


def probe_url(url):
    import yt_dlp
    try:
        info = ydl_run(lambda y: y.extract_info(url, download=False), {"skip_download": True, "extract_flat": False})
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        return {"ok": True, "platform": platform_of(url, info.get("extractor_key")), "title": info.get("title") or "",
                "uploader": info.get("uploader") or info.get("channel") or "", "kind": kind_of(info, url),
                "duration": info.get("duration")}
    except Exception as e:
        return {"ok": False, "platform": platform_of(url), "error": friendly_error(e)}


LIVE_FORMAT = "bv*[height<=1080][protocol*=m3u8]+ba[protocol*=m3u8]/b[height<=1080][protocol*=m3u8]/bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"


def resolve_live(url):
    """Direct stream URLs for a live broadcast so ffmpeg can record it itself.
    Returns (inputs, info) where inputs is a list of (stream_url, headers) — one muxed stream, or video + audio."""
    import yt_dlp
    info = ydl_run(lambda y: y.extract_info(url, download=False), {"format": LIVE_FORMAT, "skip_download": True})
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)
    fmts = info.get("requested_formats") or [info]
    inputs = [(f["url"], f.get("http_headers") or info.get("http_headers") or {}) for f in fmts if f.get("url")]
    if not inputs:
        raise RuntimeError("No recordable stream was found for this link")
    return inputs, info


PIPE_SITES = ("twitch.tv", "kick.com")


def ytdlp_pipe_cmd(url):
    """yt-dlp command that writes the live stream to stdout. Used for sites where ffmpeg alone stalls (Twitch, Kick):
    yt-dlp's own HLS downloader copes with their ads and playlist quirks."""
    from config import PYEXE
    s = load_settings()
    cmd = [PYEXE, "-m", "yt_dlp", "-q", "--no-warnings", "--no-part", "--hls-use-mpegts",
           "-f", "b[height<=1080]/best", "-o", "-"]
    if FFMPEG:
        cmd += ["--ffmpeg-location", str(pathlib.Path(FFMPEG).parent)]
    if os.name == "nt":
        cmd += ["--compat-options", "no-certifi"]
    if s.get("cookies_browser") and not _no_cookies["on"]:
        cmd += ["--cookies-from-browser", s["cookies_browser"]]
    return cmd + [url]


def ffmpeg_inputs(inputs):
    """ffmpeg arguments for the resolved live inputs, with reconnects for flaky streams."""
    args, maps = [], []
    for k, (u, h) in enumerate(inputs):
        hdr = "".join(f"{a}: {b}\r\n" for a, b in h.items() if a.lower() in ("user-agent", "referer", "origin", "cookie"))
        if hdr:
            args += ["-headers", hdr]
        if ".m3u8" in u or "m3u8" in u.split("?")[0]:
            args += ["-live_start_index", "-1"]
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10", "-rw_timeout", "30000000", "-i", u]
    if len(inputs) == 1:
        maps = ["-map", "0:v:0?", "-map", "0:a:0?"]
    else:
        maps = ["-map", "0:v:0", "-map", "1:a:0"]
    return args, maps


def download(url, dest_dir, log, progress, want_chat=False):
    """Downloads to dest_dir/source.mp4. Returns (path, info)."""
    import yt_dlp
    dest_dir = pathlib.Path(dest_dir)

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                progress(min(1, d.get("downloaded_bytes", 0) / tot))
    opts = _ydl_opts({
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4", "outtmpl": str(dest_dir / "source.%(ext)s"), "progress_hooks": [hook],
        "retries": 5, "fragment_retries": 10, "concurrent_fragment_downloads": 4,
    })
    chat_thread = None
    if want_chat and "youtu" in url:
        chat_thread = threading.Thread(target=_download_chat, args=(url, dest_dir), daemon=True)
        chat_thread.start()
    info = ydl_run(lambda y: y.extract_info(url, download=True), {k: v for k, v in opts.items()
                                                                   if k not in ("cookiesfrombrowser",)})
    path = next((p for p in dest_dir.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm", ".mov")), None)
    if not path:
        raise RuntimeError("Download finished but no video file was found")
    if chat_thread:
        log("Reading the chat replay…")
        chat_thread.join(timeout=240)
    return path, info


def _download_chat(url, dest_dir):
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_ydl_opts({"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
                                         "outtmpl": str(pathlib.Path(dest_dir) / "chat.%(ext)s")})) as y:
            y.download([url])
    except Exception:
        pass


def chat_density(dest_dir, bucket=10):
    """Messages per `bucket` seconds from a YouTube live_chat replay, or {}."""
    p = next(pathlib.Path(dest_dir).glob("chat*.json"), None)
    if not p:
        return {}
    counts = {}
    for line in p.read_text("utf-8", "ignore").splitlines():
        try:
            j = json.loads(line)
            ms = int(j["replayChatItemAction"]["videoOffsetTimeMsec"])
        except Exception:
            continue
        counts[ms // 1000 // bucket] = counts.get(ms // 1000 // bucket, 0) + 1
    return counts


def inopts(path):
    """Live parts (.ts) can start with seconds of picture before the sound, so look further in to find both."""
    return ["-probesize", "100M", "-analyzeduration", "60M"] if str(path).lower().endswith(".ts") else []


def ffprobe(path):
    r = run([FFPROBE, "-v", "error"] + inopts(path) + ["-print_format", "json", "-show_streams", "-show_format", str(path)], text=True)
    j = json.loads(r.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(j.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    w, h = (int(v["width"]), int(v["height"])) if v else (0, 0)
    rot = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        rot = int((v.get("tags") or {}).get("rotate", rot))
    if abs(rot) in (90, 270):
        w, h = h, w
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d or 1) or 30.0
    return {"duration": dur, "width": w, "height": h, "has_audio": a is not None, "fps": fps}


def extract_audio(src, wav):
    # gaps in the audio are filled with silence by timestamp, so transcript times match the video (captions stay in sync)
    run([FFMPEG, "-y", "-v", "error"] + inopts(src) + ["-i", str(src), "-vn", "-af", "aresample=async=1000:first_pts=0",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)


def normalize_part(ts, out):
    """Turns a recorded live part into a normal mp4 with picture and sound lined up from 0.
    Separate video and audio feeds rarely start at the same moment, so the sound is padded or trimmed by the exact gap."""
    ts, out = pathlib.Path(ts), pathlib.Path(out)
    v0, a0 = first_pts(ts, "v"), first_pts(ts, "a")
    tmp = out.with_name(out.stem + ".v.mp4")
    run([FFMPEG, "-y", "-v", "error"] + inopts(ts) + ["-i", str(ts), "-map", "0:v:0", "-c", "copy", "-an",
         "-bsf:v", "setts=ts=TS-STARTDTS", str(tmp)], check=True)
    if a0 is None or v0 is None:
        os.replace(tmp, out)
        return out
    d = a0 - v0
    af = f"asetpts=PTS-STARTPTS,adelay={int(round(d * 1000))}:all=1" if d > 0 else f"atrim=start={-d:.3f},asetpts=PTS-STARTPTS"
    wav = out.with_name(out.stem + ".a.wav")
    run([FFMPEG, "-y", "-v", "error"] + inopts(ts) + ["-i", str(ts), "-map", "0:a:0", "-af", "aresample=async=1000",
         "-ac", "2", "-ar", "48000", str(wav)], check=True)
    run([FFMPEG, "-y", "-v", "error", "-i", str(tmp), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-af", af, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], check=True)
    tmp.unlink(missing_ok=True); wav.unlink(missing_ok=True)
    return out


def first_pts(path, kind):
    """Timestamp of the first video ('v') or audio ('a') packet, or None."""
    r = run([FFPROBE, "-v", "error"] + inopts(path) + ["-select_streams", f"{kind}:0", "-show_entries", "packet=pts_time", "-read_intervals", "%+#1",
             "-of", "csv=p=0", str(path)], text=True)
    try:
        return float((r.stdout or "").split(",")[0].split()[0])
    except Exception:
        return None


def loudness(wav, hop=1.0):
    """RMS loudness (dB) per `hop` seconds."""
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    n = int(sr * hop)
    if len(x) < n:
        return np.array([-60.0])
    x = x[: len(x) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((x ** 2).mean(1)) + 1e-6)


def thumb(src, t, out, width=360):
    run([FFMPEG, "-y", "-v", "error"] + inopts(src) + ["-ss", str(max(0, t)), "-i", str(src), "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])
