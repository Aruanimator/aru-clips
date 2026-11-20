"""Projects: the clipping pipeline, live-stream clipping, re-renders and the Facebook / Instagram export pack."""
import json, os, re, shutil, sys, time, threading, traceback, pathlib, uuid, subprocess
import numpy as np
from config import output_dir, load_settings, slug, FFMPEG, popen, run, UPLOADS
import ai, media, picker, render, signals

JOBS = {}
_pipe_lock = threading.Lock()   # one download/transcribe pipeline at a time
_render_lock = threading.Lock()  # one render at a time keeps the PC responsive


class Job:
    def __init__(self, jid, d, data=None):
        self.id, self.dir = jid, pathlib.Path(d)
        self.data = data or {}
        self.lock = threading.RLock()
        self.stop = threading.Event()

    # ---- state
    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.save()

    def log(self, msg):
        with self.lock:
            logs = self.data.setdefault("logs", [])
            logs.append(time.strftime("%H:%M:%S ") + str(msg))
            del logs[:-300]
            self.save()

    def stage(self, msg, progress=None):
        with self.lock:
            self.data["stage"] = msg
            if progress is not None:
                self.data["progress"] = round(max(0, min(1, progress)), 4)
            self.log(msg)

    def save(self):
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.dir / "job.json")

    def public(self):
        with self.lock:
            d = dict(self.data)
            d["id"] = self.id
            d["dir"] = str(self.dir).replace("\\", "/")
            d["clips"] = [dict(c) for c in d.get("clips", [])]
            d.setdefault("logs", []); d.setdefault("error", None); d.setdefault("progress", 0)
            return d

    def clip(self, n):
        return next((c for c in self.data.get("clips", []) if c["n"] == n), None)

    def rel(self, p):
        return f"{self.id}/{pathlib.Path(p).name}"


def load_all():
    root = output_dir()
    for jf in root.glob("*/job.json"):
        jid = jf.parent.name
        if jid in JOBS:
            continue
        try:
            data = json.loads(jf.read_text("utf-8"))
        except Exception:
            continue
        if data.get("status") in ("running", "queued"):
            data["status"] = "error"; data["error"] = "The app was closed while this project was running."
        for c in data.get("clips", []):
            c.pop("rendering", None)
        JOBS[jid] = Job(jid, jf.parent, data)


def get(jid):
    if jid not in JOBS:
        load_all()
    return JOBS.get(jid)


def listing():
    load_all()
    out = []
    for j in JOBS.values():
        d = j.data
        clips = d.get("clips", [])
        out.append({"id": j.id, "title": d.get("title", ""), "status": d.get("status"), "created": d.get("created", 0),
                    "count": len([c for c in clips if c.get("file")]),
                    "thumb": next((c["thumb"] for c in clips if c.get("thumb")), "")})
    return sorted(out, key=lambda x: -x["created"])


def create(opts):
    title = opts.get("title") or ""
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    d = output_dir() / jid
    d.mkdir(parents=True)
    j = Job(jid, d, {"status": "queued", "stage": "Waiting to start…", "progress": 0, "logs": [], "clips": [], "error": None,
                     "title": title or opts.get("url") or "New project", "created": time.time(), "opts": opts, "export": {}})
    j.save()
    JOBS[jid] = j
    target = run_live if opts.get("source_type") == "live" else run_job
    threading.Thread(target=_guard, args=(j, target), daemon=True).start()
    return jid


def _guard(j, fn):
    try:
        fn(j)
    except Exception as e:
        traceback.print_exc()
        msg = media.friendly_error(e) if "yt_dlp" in type(e).__module__ or "Download" in type(e).__name__ else str(e)
        j.log(traceback.format_exc()[-1500:])
        j.set(status="error", error=msg[:500], stage="Something went wrong")


# ======================================================================= pipeline
def _clip_opts(j, c):
    o = j.get("opts", {})
    return {"crop_mode": c.get("crop_mode") or o.get("crop_mode"), "format": c.get("format") or o.get("format", "9:16"),
            "caption_style": c.get("caption_style") or o.get("caption_style"), "caption_position": c.get("caption_position", o.get("caption_position", 0.7)),
            "title": c.get("title", ""), "title_overlay": c.get("title_overlay", o.get("title_overlay", True)),
            "remove_silence": c.get("remove_silence", o.get("remove_silence", False)),
            "look": {**(o.get("look") or {}), **(c.get("look") or {})}, "sfx": o.get("sfx") or {},
            "layout": {**(o.get("layout") or {}), **(c.get("layout") or {})}, "brand": o.get("brand") or {},
            "framing": {"panes": c["framing_manual"]} if c.get("framing_manual") else None,
            "cam_cache": str(j.dir / "cam.json"),
            "stream": o.get("content") == "gaming" or o.get("source_type") in ("stream", "live")}


def _words_for(j, c, edited=True):
    if edited and c.get("words"):  # caption text edited in the clip editor
        return c["words"], j.get("lang", "en")
    name = (c.get("seg") or "transcript") + ".json"
    p = j.dir / ("live" if c.get("seg") else "") / name if c.get("seg") else j.dir / name
    try:
        tr = json.loads(p.read_text("utf-8"))
    except Exception:
        return [], j.get("lang", "en")
    words = tr.get("words", [])
    lang = tr.get("language", "en")
    look = (j.get("opts") or {}).get("look") or {}
    if look.get("banglish") and lang == "bn":
        cache = p.with_name(p.stem + ".banglish.json")
        if cache.exists():
            words = json.loads(cache.read_text("utf-8"))
        else:
            words = picker.banglish(words, log=j.log)
            cache.write_text(json.dumps(words, ensure_ascii=False), "utf-8")
    return words, lang


def _src_for(j, c):
    if c.get("seg"):
        mp4 = j.dir / "live" / (c["seg"] + ".mp4")
        return mp4 if mp4.exists() else j.dir / "live" / (c["seg"] + ".ts")
    return next((p for p in j.dir.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts")), None)


def render_one(j, c):
    src = _src_for(j, c)
    if not src or not src.exists():
        raise RuntimeError("The source video for this clip is missing")
    words, lang = _words_for(j, c)
    out = j.dir / f"clip_{c['n']:02d}.mp4"
    with _render_lock:
        r = render.render_clip(src, out, c["start"], c["end"], words, _clip_opts(j, c), lang, j.log)
    if not j.data.get("_enc_logged"):
        j.data["_enc_logged"] = True
        j.log(f"Rendering with {r.get('encoder', 'CPU')} encoding")
    with j.lock:
        c.update(file=j.rel(out), thumb=f"/media/{j.rel(out.with_suffix('.jpg'))}?v={int(time.time())}",
                 duration=r["duration"], rendered_at=int(time.time() * 1000), pending=False, cuts=r.get("cuts"),
                 framing_auto=r.get("framing"))
        c.pop("rendering", None)
        if r["layout_used"] != c.get("crop_mode") and c.get("crop_mode") in ("face",) and r["layout_used"] == "center":
            j.log(f"Clip {c['n']}: no face found, used a center crop")
        j.save()


def _pads(o):
    """Seconds of build-up before and aftermath after each moment."""
    return float(o.get("pre_roll", 3)), float(o.get("post_roll", 4))


def run_job(j):
    j.set(status="queued", stage="Waiting for the previous project to finish…")
    with _pipe_lock:
        _run_job(j)


def _run_job(j):
    o = j.get("opts")
    j.set(status="running")
    mode = o.get("source_type") or "video"
    # 1. source
    if o.get("file"):
        up = UPLOADS / o["file"]
        if not up.exists():
            raise RuntimeError("The uploaded file is missing. Drop it again.")
        dst = j.dir / ("source" + up.suffix.lower())
        shutil.move(str(up), dst)
        src = dst
        j.set(title=o.get("title") or up.stem)
        j.stage("Video ready", 0.3)
    else:
        j.stage("Downloading the video…", 0.01)
        src, info = media.download(o["url"], j.dir, j.log, lambda f: j.set(progress=round(0.01 + 0.29 * f, 3)),
                                   want_chat=mode == "stream")
        j.set(title=info.get("title") or j.get("title"))
        if info.get("heatmap"):
            (j.dir / "heatmap.json").write_text(json.dumps(info["heatmap"]), "utf-8")
            j.log("Got YouTube's most-replayed graph for this video")
        j.stage("Downloaded", 0.3)
    meta = media.ffprobe(src)
    total = meta["duration"]
    j.log(f"Source: {meta['width']}x{meta['height']}, {int(total // 60)}m{int(total % 60)}s")
    tw_chat = {}
    tw_thread = None
    if o.get("url") and "twitch.tv" in o["url"] and signals.twitch_video_id(o["url"]):
        j.log("Reading the Twitch chat replay…")
        tw_thread = threading.Thread(target=lambda: tw_chat.update(m=signals.twitch_replay_chat(o["url"], total, j.log)), daemon=True)
        tw_thread.start()
    # 2. audio + transcript
    wav = j.dir / "audio.wav"
    tr = _transcribe(j, src, wav, total, o.get("language", "auto"), 0.3, 0.6)
    (j.dir / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False), "utf-8")
    j.set(lang=tr["language"])
    # 3. audience signals + picking
    loud = media.loudness(wav) if meta["has_audio"] else np.array([-30.0])
    if tw_thread:
        j.stage("Finishing the Twitch chat replay…", 0.61)
        tw_thread.join()
    chat = tw_chat.get("m") or signals.yt_replay_chat(j.dir)
    if chat and not tw_chat.get("m"):
        j.log(f"Read {len(chat):,} YouTube chat messages")
    heat = json.loads((j.dir / "heatmap.json").read_text("utf-8")) if (j.dir / "heatmap.json").exists() else None
    want_split = o.get("crop_mode") == "split2"
    ok = None
    if want_split:
        j.stage("Checking where the streamer is on screen…", 0.615)
        try:
            ok = render.split_timeline(src, total, j.log)
        except Exception as e:
            j.log(f"Couldn't check the streamer's screen time ({str(e)[:120]})")
    n_want = int(o.get("num_clips", 6))
    j.stage("Finding the best moments…", 0.62)
    clips, eng = picker.pick_audience(tr, total, n_want * 3 if ok else n_want, float(o.get("min_len", 20)), float(o.get("max_len", 60)),
                                      mode, o.get("prompt", ""), chat, heat, loud, j.log,
                                      lambda f: j.set(progress=round(0.62 + 0.08 * f, 3)),
                                      o.get("content", "general"), o.get("game", "any"), 6.0, None, *_pads(o))
    if ok:
        clips = _prefer_split(clips, ok, n_want, float(o.get("min_len", 20)), j.log)
        clips = [picker.finish_sentence(c, tr["words"], tr["segments"], float(o.get("min_len", 20)), float(o.get("max_len", 60)), total)
                 for c in clips]  # moved clips still end where a sentence does
    j.log(f"Picked {len(clips)} clips" + (f" with {eng}" if eng and eng != "heuristic" else ""))
    if not clips:
        j.set(status="done", stage="No clips were found in this video.", progress=1)
        return
    # 4. render
    base = {"crop_mode": o.get("crop_mode", "face"), "caption_style": o.get("caption_style", "bold"),
            "caption_position": o.get("caption_position", 0.7), "title_overlay": o.get("title_overlay", True), "format": o.get("format", "9:16"),
            "remove_silence": bool(o.get("remove_silence"))}
    clips.sort(key=lambda c: -c["score"])
    for k, c in enumerate(clips):
        c.update(base, n=k + 1)
        j.stage(f"Rendering clip {k + 1} of {len(clips)}: {c['title'][:50]}", 0.7 + 0.3 * k / len(clips))
        with j.lock:
            j.data["clips"].append(c)
        try:
            render_one(j, c)
        except Exception as e:
            j.log(f"Clip {k + 1} failed: {str(e)[:300]}")
            with j.lock:
                j.data["clips"].remove(c)
    for p in j.dir.glob("*_part_*.*"):
        p.unlink(missing_ok=True)
    n = len([c for c in j.data["clips"] if c.get("file")])
    j.set(status="done", stage=f"Done! {n} clips saved.", progress=1)


def _prefer_split(clips, ok, n, mn, log):
    """2-frame split on an edited video: keep the moments where the streamer and the game are on screen together,
    nudging each clip a few seconds to cover as much of that as possible."""
    scored = []
    for c in clips:
        a, b, cov = render.best_split_window(ok, c, max(8.0, mn * 0.8))
        if (a, b) != (c["start"], c["end"]):
            c = dict(c, start=round(a, 2), end=round(b, 2), duration=round(b - a, 2))
        c["split_cover"] = cov
        scored.append((c["score"] * (0.35 + 0.65 * cov) + (15 if cov >= 0.75 else 0), c))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, c in scored:
        if all(c["end"] <= o["start"] + 0.5 or c["start"] >= o["end"] - 0.5 for o in out):
            out.append(c)
        if len(out) >= n:
            break
    good = sum(1 for c in out if c["split_cover"] >= 0.75)
    log(f"{good} of {len(out)} clips keep the streamer and the game on screen together the whole way (or nearly)")
    return out


def _transcribe(j, src, wav, total, lang, p0, p1):
    j.stage("Getting the audio…", p0)
    media.extract_audio(src, wav)
    s = load_settings()
    who = "Groq Whisper" if ai.has_groq(s) else "Whisper on this PC"
    j.stage(f"Transcribing with {who}…", p0 + 0.01)
    tr = ai.transcribe(wav, total, lang, j.log, lambda f: j.set(progress=round(p0 + 0.01 + (p1 - p0 - 0.01) * f, 3)))
    j.log(f"Transcribed {len(tr['words'])} words ({tr['language']}) with {tr['engine']}")
    return tr


# ======================================================================= live
def run_live(j):
    o = j.get("opts")
    lv = o.get("live") or {}
    chunk = max(3, int(lv.get("chunk_min", 10))) * 60
    total_min = int(lv.get("record_min", 60))
    per = int(lv.get("per_chunk", 2))
    review = bool(lv.get("review", True))
    auto_score = int(lv.get("auto_score") or 0)  # with review on: clips scoring this or higher render without asking
    ldir = j.dir / "live"; ldir.mkdir(exist_ok=True)
    s = load_settings()
    probe = media.probe_url(o["url"])
    j.set(status="running", title="LIVE · " + (probe.get("title") or o["url"]), live={"recording": True, "elapsed": 0, "parts": 0})
    j.stage("Connecting to the live stream…", 0.02)
    ylog = open(ldir / "recorder.log", "ab")
    rec_state = {"proc": None, "restarts": 0}

    def start_recorder(first=None):
        """Resolve the stream and let ffmpeg record it straight into parts (no piping, so any format works).
        first: length of the first part, used after a reconnect to finish the batch that the drop cut short."""
        try:
            inputs, info = media.resolve_live(o["url"])
        except Exception as e:
            ylog.write(("resolve: " + str(e) + "\n").encode("utf-8", "ignore")); ylog.flush()
            return False
        if info.get("live_status") not in (None, "is_live") and not info.get("is_live"):
            ylog.write(b"not live\n"); ylog.flush()
            raise RuntimeError("This link isn't live right now. Use Past stream for a finished broadcast.")
        args, maps = media.ffmpeg_inputs(inputs)
        nums = [int(m.group(1)) for q in ldir.glob("seg_*") if (m := re.match(r"seg_(\d+)\.(ts|mp4)$", q.name))]
        start_no = max(nums) + 1 if nums else 0
        # -copyts keeps the stream's own clock, so separate video and audio feeds stay in sync
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-copyts"] + args + maps + [
            "-c", "copy", "-f", "segment"] + (
            ["-segment_times", ",".join(f"{first + k * chunk:.0f}" for k in range(400))] if first else ["-segment_time", str(chunk)]) + [
            "-segment_start_number", str(start_no), str(ldir / "seg_%03d.ts")]
        rec_state["proc"] = popen(cmd, stdin=subprocess.PIPE, stderr=ylog)
        return True

    def stop_recorder():
        p = rec_state["proc"]
        if not p or p.poll() is not None:
            return
        try:
            p.stdin.write(b"q"); p.stdin.flush()  # lets ffmpeg close the last part cleanly
            p.wait(timeout=20)
        except Exception:
            p.kill()

    chat_reader = signals.start_live_chat(o["url"], ldir, j.log)
    first_seen, watch_stop = {}, threading.Event()

    def watch():  # wall-clock start of each part, so chat lines up even while a part is being processed
        while not watch_stop.is_set():
            for q in ldir.glob("seg_*.ts"):
                first_seen.setdefault(q.stem, time.time())
            time.sleep(1)
    threading.Thread(target=watch, daemon=True).start()
    if not start_recorder():
        watch_stop.set()
        if chat_reader: chat_reader.stop.set()
        ylog.close()
        err = (ldir / "recorder.log").read_text("utf-8", "ignore")[-400:]
        raise RuntimeError("Could not open this live stream. " + media.friendly_error(err.replace("resolve: ", "")))
    t0, done, offsets, durs, ncl = time.time(), set(), {}, {}, [0]
    spike_ranges = []          # wall-clock ranges already covered by instant clips
    ai_lock = threading.Lock()  # parts and instant clips take turns on the AI
    mn, mx = float(o.get("min_len", 20)), float(o.get("max_len", 60))

    def clip_defaults(c):
        c.update(crop_mode=o.get("crop_mode", "face"), caption_style=o.get("caption_style", "bold"),
                 caption_position=o.get("caption_position", 0.7), title_overlay=o.get("title_overlay", True),
                 format=o.get("format", "9:16"), remove_silence=bool(o.get("remove_silence")))

    def add_clip(c, src_file):
        with j.lock:
            ncl[0] += 1
            c["n"] = ncl[0]
        if review and auto_score and int(c.get("score", 0)) >= auto_score:
            j.log(f"Clip {c['n']} scored {c.get('score')}: auto-approved (your bar is {auto_score})")
        if review and not (auto_score and int(c.get("score", 0)) >= auto_score):
            th = j.dir / f"pending_{c['n']:02d}.jpg"
            media.thumb(src_file, c["start"] + 1, th)
            c.update(pending=True, thumb=f"/media/{j.rel(th)}")
            with j.lock:
                j.data["clips"].append(c); j.save()
        else:
            with j.lock:
                j.data["clips"].append(c)
            try:
                render_one(j, c)
            except Exception as e:
                j.log(f"Clip {c['n']} failed: {str(e)[:200]}")
                with j.lock:
                    j.data["clips"].remove(c)

    seg_dur = {}

    def timeline_now():
        """[(name, start, duration)] of the recording so far, anchored at the live edge."""
        names = sorted(q.stem for q in ldir.glob("seg_*.ts"))
        out, t = [], 0.0
        for i, nm in enumerate(names):
            if nm not in seg_dur or i == len(names) - 1:
                d = media.ffprobe(ldir / (nm + ".ts"))["duration"]
                if i < len(names) - 1:
                    seg_dur[nm] = d
            else:
                d = seg_dur[nm]
            out.append((nm, t, d)); t += d
        return out, t

    def instant_clip(k, moment, ratio, words):
        """Cuts the moments around a chat explosion out of the recording and turns it into a clip right away.
        The newest recorded second is 'now', so a wall-clock moment maps to (recorded so far) - (now - moment)."""
        win = max(40.0, mn + 12)
        now = rec_state.get("end") or time.time()  # after the recording stops, its last second is the anchor
        segs, total = timeline_now()
        if not segs:
            return
        g_a = max(0.0, total - (now - (moment - 0.65 * win)))
        g_b = min(total, total - (now - (moment + 0.35 * win)))
        use = [x for x in segs if x[1] + x[2] > g_a and x[1] < g_b]
        if not use or g_b - g_a < 6:
            j.log(f"Instant clip {k}: couldn't cut the recording yet"); return
        name = f"spike_{k:03d}"
        raw = ldir / (name + ".ts")
        # Seeking inside live parts only lands on keyframes, so seek roughly (30 s early) and trim on the stream's own
        # clock: the recording keeps original timestamps, so both picture and sound are cut on the exact second.
        firsts = [x for x in (media.first_pts(ldir / "seg_000.ts", "v"), media.first_pts(ldir / "seg_000.ts", "a")) if x is not None]
        p0 = min(firsts) if firsts else 0.0
        abs_a, abs_b = p0 + g_a, p0 + g_b
        rough = max(0.0, (g_a - use[0][1]) - 30)
        run([FFMPEG, "-y", "-v", "error"] + media.inopts(raw) + ["-copyts", "-ss", f"{rough:.2f}", "-i",
             "concat:" + "|".join(x[0] + ".ts" for x in use),
             "-vf", f"trim=start={abs_a:.3f}:end={abs_b:.3f},setpts=PTS-STARTPTS",
             "-af", f"atrim=start={abs_a:.3f}:end={abs_b:.3f},asetpts=PTS-STARTPTS",
             "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
             str(raw)], cwd=str(ldir))
        if not raw.exists() or raw.stat().st_size < 10000:
            j.log(f"Instant clip {k}: couldn't cut the recording yet"); return
        p = media.normalize_part(raw, ldir / (name + ".mp4"))
        raw.unlink(missing_ok=True)
        meta = media.ffprobe(p)
        if meta["duration"] < 6:
            return
        w0 = now - (total - g_a)  # wall-clock time of the clip's first frame
        wav = ldir / (name + ".wav")
        media.extract_audio(p, wav)
        with ai_lock:
            tr = ai.transcribe(wav, meta["duration"], o.get("language", "auto"), j.log)
            (ldir / (name + ".json")).write_text(json.dumps(tr, ensure_ascii=False), "utf-8")
            chat = [(t - w0, x) for t, x in list(chat_reader.msgs) if w0 - 5 <= t <= w0 + meta["duration"] + 30]
            clips, _ = picker.pick_audience(tr, meta["duration"], 1, min(mn, meta["duration"] * 0.6), min(mx, meta["duration"]),
                                            "live", o.get("prompt", ""), chat, None, media.loudness(wav), j.log, lambda f: None,
                                            o.get("content", "general"), o.get("game", "any"), 6.0, None, *_pads(o))
        wav.unlink(missing_ok=True)
        for q in ldir.glob(name + "_part_*.*"):
            q.unlink(missing_ok=True)
        for c in clips[:1]:
            clip_defaults(c)
            c.update(seg=name, offset=round(g_a, 2), instant=True)
            c["reason"] = (c.get("reason", "") + f" Chat exploded: {ratio:.1f}x normal ({', '.join(words)}).").strip()
            spike_ranges.append((w0 + c["start"], w0 + c["end"]))
            add_clip(c, p)
            j.log(f"⚡ Instant clip from a chat spike ({ratio:.1f}x normal): {c['title'][:60]}")

    def spike_watch():
        queue, last, k = [], 0.0, 0
        while not watch_stop.is_set() or queue:
            now = time.time()
            if not watch_stop.is_set() and chat_reader.msgs and now - last >= 45 and now - t0 > 45:
                hit = signals.spike_now(list(chat_reader.msgs), now)
                if hit:
                    last, k = now, k + 1
                    moment = now - 3 - 5 - 6  # middle of the 10 s window, minus the time chat takes to react
                    words = signals.top_words(chat_reader.msgs, now - 13, now - 3)
                    queue.append((k, moment, hit[1], words, moment + 0.35 * max(40.0, mn + 12) + 14))
                    j.log(f"Chat spike! {hit[0]} messages in 10 s, {hit[1]:.1f}x normal ({', '.join(words)})")
            for item in [q for q in queue if q[4] <= now or watch_stop.is_set()]:
                queue.remove(item)
                try:
                    k_, moment, ratio, words = item[:4]
                    # refine: the action happened a few seconds before chat started to explode
                    det = moment + 14
                    bins = {}
                    for t, x in list(chat_reader.msgs):
                        if det - 25 <= t <= det + 15:
                            bins[int(t)] = bins.get(int(t), 0) + signals.msg_weight(x)
                    if bins:
                        ts_ = range(int(det) - 25, int(det) + 16)
                        sm = {t: sum(bins.get(u, 0) for u in range(t - 2, t + 3)) / 5 for t in ts_}
                        peak = max(sm.values())
                        onset = next((t for t in ts_ if sm[t] >= 0.5 * peak), int(det))
                        moment = onset - 4
                    instant_clip(k_, moment, ratio, words)
                except Exception as e:
                    j.log(f"Instant clip failed: {str(e)[:200]}")
            time.sleep(1)

    spike_thread = None
    if chat_reader and lv.get("instant", True):
        spike_thread = threading.Thread(target=spike_watch, daemon=True)
        spike_thread.start()

    carry = []  # parts cut short by a dropped connection, waiting to be joined with the rest of their batch
    batches = [0]

    def joined(names):
        """Joins pieces of one batch (before and after a reconnect) into one aligned part."""
        parts = [media.normalize_part(ldir / (nm + ".ts"), ldir / (nm + ".mp4")) for nm in names]
        out = ldir / (names[0] + "_m.mp4")
        lst = ldir / (names[0] + "_m.txt")
        lst.write_text("".join(f"file '{q.name}'\n" for q in parts), "utf-8")
        run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)],
            cwd=str(ldir))
        lst.unlink(missing_ok=True)
        if not out.exists() or out.stat().st_size < 10000:
            raise RuntimeError("couldn't join the parts around the reconnect")
        return names[0] + "_m", out

    def process(name, ready=None):
        p = ready or media.normalize_part(ldir / (name + ".ts"), ldir / (name + ".mp4"))
        meta = media.ffprobe(p)
        prev = sorted(k for k in offsets if k < name)
        offsets[name] = (offsets[prev[-1]] + durs.get(prev[-1], 0)) if prev else 0.0
        durs[name] = meta["duration"]
        if meta["duration"] < 8:
            return
        wav = ldir / (name + ".wav")
        media.extract_audio(p, wav)
        with ai_lock:
            tr = ai.transcribe(wav, meta["duration"], o.get("language", "auto"), j.log)
        (ldir / (name + ".json")).write_text(json.dumps(tr, ensure_ascii=False), "utf-8")
        j.set(lang=tr["language"])
        loud = media.loudness(wav)
        chat, base = None, None
        if chat_reader and chat_reader.msgs and name in first_seen:
            w0, msgs = first_seen[name], list(chat_reader.msgs)
            chat = [(t - w0, txt) for t, txt in msgs if w0 - 5 <= t <= w0 + meta["duration"] + 30]
            span = max(60.0, msgs[-1][0] - msgs[0][0])
            base = max(0.05, 0.8 * sum(signals.msg_weight(x) for _, x in msgs) / span)
            j.log(f"Part {batches[0]}: {len(chat):,} chat messages")
        with ai_lock:
            clips, _ = picker.pick_audience(tr, meta["duration"], per, min(mn, meta["duration"] * 0.5), min(mx, meta["duration"]),
                                            "live", o.get("prompt", ""), chat, None, loud, j.log, lambda f: None,
                                            o.get("content", "general"), o.get("game", "any"), 6.0, base, *_pads(o))
        w0 = first_seen.get(name)
        for c in clips:
            if w0 and any(min(b, w0 + c["end"]) - max(a, w0 + c["start"]) > 0.5 * (c["end"] - c["start"]) for a, b in spike_ranges):
                continue  # an instant clip already has this moment
            clip_defaults(c)
            c.update(seg=name, offset=round(offsets[name], 2))
            add_clip(c, p)
        wav.unlink(missing_ok=True)
        for q in ldir.glob(name + "_part_*.*"):
            q.unlink(missing_ok=True)

    def status(rec_on):
        el = int(time.time() - t0)
        n = len(j.data.get("clips", []))
        j.set(live={"recording": rec_on, "elapsed": el, "parts": len(done)})
        lim = f" of {total_min} min" if total_min else ""
        inst = len([c for c in j.data.get("clips", []) if c.get("instant")])
        j.stage(f"● Recording live · {el // 60}:{el % 60:02d}{lim} · {n} clips so far"
                + (f" ({inst} instant from chat spikes)" if inst else "") + f" · a new batch every {chunk // 60} min",
                (el / (total_min * 60)) * 0.95 if total_min else 0.4)

    try:
        last_status = 0
        while True:
            segs = sorted(p.stem for p in ldir.glob("seg_*.ts"))
            for name in segs[:-1]:
                if name not in done:
                    done.add(name)
                    batches[0] += 1
                    j.log(f"Finding clips in part {batches[0]}…")
                    try:
                        if carry:  # the batch a dropped connection cut short: join its pieces, then find clips
                            nm, ready = joined(carry + [name])
                            first_seen.setdefault(nm, first_seen.get(carry[0], time.time()))
                            carry.clear()
                            process(nm, ready)
                        else:
                            process(name)
                    except Exception as e:
                        j.log(f"Part {name} failed: {str(e)[:300]}")
            if time.time() - last_status > 5:
                status(True); last_status = time.time()
            if j.stop.is_set() or (total_min and time.time() - t0 >= total_min * 60):
                break
            p = rec_state["proc"]
            if p.poll() is not None:
                # internet blips are common: keep trying to reconnect for up to 5 minutes before giving up
                dropped, back = time.time(), False
                j.log("The stream connection dropped, reconnecting…")
                left = None
                cut = sorted(q.stem for q in ldir.glob("seg_*.ts"))
                if cut and cut[-1] not in done:
                    try:
                        d = media.ffprobe(ldir / (cut[-1] + ".ts"))["duration"] or 0
                    except Exception:
                        d = 0
                    have = d + sum((media.ffprobe(ldir / (x + ".ts"))["duration"] or 0) for x in carry)
                    if d > 0 and have < chunk * 0.9:
                        carry.append(cut[-1]); done.add(cut[-1])  # wait for the rest of this batch
                        left = max(20.0, chunk - have)
                        j.log(f"Holding the {int(have // 60)}:{int(have % 60):02d} recorded so far; this batch continues after the reconnect")
                while time.time() - dropped < 300 and not j.stop.is_set():
                    time.sleep(min(30, 5 + 5 * rec_state["restarts"]))
                    rec_state["restarts"] += 1
                    try:
                        if start_recorder(left):
                            back = True; break
                    except RuntimeError:
                        break  # the stream really ended
                if back:
                    j.log("Reconnected to the stream")
                    continue
                j.log("The stream ended or the connection didn't come back.")
                break
            time.sleep(2)
    finally:
        stop_recorder()
        rec_state["end"] = time.time()
        ylog.close()
        watch_stop.set()
        if chat_reader:
            chat_reader.stop.set()
    j.set(live={"recording": False, "elapsed": int(time.time() - t0), "parts": len(done)})
    j.stage("Stopping. Finishing the last clips…")
    if spike_thread:
        spike_thread.join(timeout=900)
    rest = [nm for nm in sorted(p.stem for p in ldir.glob("seg_*.ts")) if nm not in done]
    if carry:
        rest = carry[:] + rest
    if carry and len(rest) > 1:
        for nm in rest:
            done.add(nm)
        try:
            nm, ready = joined(rest[:len(carry) + 1])
            first_seen.setdefault(nm, first_seen.get(rest[0], time.time()))
            process(nm, ready)
        except Exception as e:
            j.log(f"Part {rest[0]} failed: {str(e)[:300]}")
        rest = rest[len(carry) + 1:]
        carry.clear()
    for name in rest:
        if name not in done or name in carry:
            done.add(name)
            try:
                process(name)
            except Exception as e:
                j.log(f"Part {name} failed: {str(e)[:300]}")
    for q in ldir.glob("seg_*.ts"):  # parts are kept as aligned .mp4 files
        if q.with_suffix(".mp4").exists():
            q.unlink(missing_ok=True)
    if not done:
        err = (ldir / "recorder.log").read_text("utf-8", "ignore")[-400:]
        raise RuntimeError("Could not record this stream. " + (media.friendly_error(err) if err.strip() else "Is it live right now?"))
    pend = len([c for c in j.data["clips"] if c.get("pending")])
    n = len(j.data["clips"])
    j.set(status="done", progress=1,
          stage=f"Recording finished. {pend} clips are waiting for your approval." if pend else f"Done! {n} clips saved.")


# ======================================================================= actions
def approve(j, n):
    c = j.clip(n)
    if not c:
        raise KeyError("clip")
    with j.lock:
        c["rendering"] = True; j.save()
    try:
        render_one(j, c)
    finally:
        with j.lock:
            c.pop("rendering", None); j.save()
    pend = j.dir / f"pending_{n:02d}.jpg"
    pend.unlink(missing_ok=True)


def reject(j, n):
    with j.lock:
        j.data["clips"] = [c for c in j.data["clips"] if c["n"] != n]
        j.save()
    (j.dir / f"pending_{n:02d}.jpg").unlink(missing_ok=True)


def rerender(j, n, body):
    c = j.clip(n)
    if not c:
        raise KeyError("clip")
    with j.lock:
        for k in ("title", "crop_mode", "format", "caption_style", "caption_position", "title_overlay", "remove_silence", "caption"):
            if k in body and body[k] is not None:
                c[k] = body[k]
        if isinstance(body.get("hashtags"), list):  # post text for TikTok / Reels / Shorts
            c["hashtags"] = [h if str(h).startswith("#") else "#" + str(h) for h in body["hashtags"] if str(h).strip()][:12]
        for k in ("look", "layout"):  # per-clip style / layout overrides from the editor
            if isinstance(body.get(k), dict):
                c[k] = {**(c.get(k) or {}), **body[k]}
        if "framing" in body:  # manual framing of each screen; null goes back to automatic
            c["framing_manual"] = body["framing"] or None
            if not c["framing_manual"]:
                c.pop("framing_manual", None)
        if "words" in body:  # edited caption text; null goes back to the transcript
            if body["words"]:
                c["words"] = [dict({"w": str(w["w"]), "s": float(w["s"]), "e": float(w["e"])},
                                   **({"line": int(w["line"])} if w.get("line") is not None else {}))
                              for w in body["words"] if str(w.get("w", "")).strip()]
            else:
                c.pop("words", None)
        s, e = float(body.get("start", c["start"])), float(body.get("end", c["end"]))
        if e - s < 1:
            raise ValueError("The clip must be at least 1 second long")
        c["start"], c["end"], c["duration"] = round(s, 2), round(e, 2), round(e - s, 1)
        c["rendering"] = True
        j.save()
    try:
        render_one(j, c)
    finally:
        with j.lock:
            c.pop("rendering", None); j.save()


def edit_info(j, n, original=False):
    """Everything the clip editor needs: the source video, words around the clip, current framing and styles."""
    c = j.clip(n)
    if not c:
        raise KeyError("clip")
    src = _src_for(j, c)
    meta = media.ffprobe(src) if src else {"duration": 0, "width": 1920, "height": 1080}
    words, lang = _words_for(j, c, edited=not original)
    a, b = c["start"] - 60, c["end"] + 60
    o = _clip_opts(j, c)
    return {"clip": c, "source": f"/media/{j.id}/{src.relative_to(j.dir).as_posix()}" if src else "",
            "src_w": meta["width"], "src_h": meta["height"], "src_dur": meta["duration"], "lang": lang,
            "words": [w for w in words if a <= w["s"] <= b], "edited_words": bool(c.get("words")),
            "framing": c.get("framing_auto") or {}, "manual": c.get("framing_manual") or {},
            "look": o["look"], "layout": o["layout"]}


def _srt(words, start, end):
    groups = render.caption_groups(words, start, end, 7)

    def ts(t):
        ms = int(round(t * 1000)); return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i + 1}\n{ts(g[0]['s'])} --> {ts(g[-1]['e'] + 0.2)}\n{' '.join(w['w'] for w in g)}\n" for i, g in enumerate(groups))


def export_pack(j, ns=None):
    with j.lock:
        if (j.get("export") or {}).get("state") == "running":
            return
        j.data["export"] = {"state": "running", "done": 0, "total": 0}; j.save()
    threading.Thread(target=_export, args=(j, ns), daemon=True).start()


def delete_clips(j, ns):
    """Removes clips (rendered or waiting) and their files from the project."""
    ns = {int(n) for n in ns}
    with j.lock:
        gone = [c for c in j.data["clips"] if c["n"] in ns]
        j.data["clips"] = [c for c in j.data["clips"] if c["n"] not in ns]
        j.save()
    for c in gone:
        if c.get("file"):
            f = output_dir() / c["file"]
            for p in (f, f.with_suffix(".jpg")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        (j.dir / f"pending_{c['n']:02d}.jpg").unlink(missing_ok=True)
    if gone:
        j.log(f"Deleted {len(gone)} clip{'s' if len(gone) != 1 else ''}")
    return len(gone)


def _export(j, ns=None):
    try:
        clips = [c for c in j.data["clips"] if c.get("file") and not c.get("pending") and (ns is None or c["n"] in ns)]
        pack = j.dir / ("facebook-instagram-pack" if ns is None else "selected-clips-pack")
        if ns is not None and pack.exists():
            shutil.rmtree(pack, ignore_errors=True)
        pack.mkdir(exist_ok=True)
        warnings = []
        lang = j.get("lang", "en")
        caps = [f"{c.get('caption') or c['title']}" for c in clips]
        other = "en" if lang == "bn" else "bn"
        trans = picker.translate_captions(caps, other, j.log) if caps else []
        if trans is None:
            warnings.append("Bangla captions need Groq or Ollama, so only the original captions were written")
            trans = [""] * len(caps)
        j.data["export"].update(total=len(clips)); j.save()
        fb_lang = {"en": "en_US", "bn": "bn_IN", "hi": "hi_IN", "ur": "ur_PK", "ar": "ar_AR", "es": "es_LA", "fr": "fr_FR",
                   "de": "de_DE", "pt": "pt_BR", "id": "id_ID"}.get(lang, "en_US")
        for i, c in enumerate(clips):
            name = f"{i + 1:02d} - {slug(c['title'], 40)}"
            shutil.copy(output_dir() / c["file"], pack / f"{name}.mp4")
            en, bn = (trans[i], caps[i]) if lang == "bn" else (caps[i], trans[i])
            tags = " ".join(c.get("hashtags") or [])
            body = f"{c['title']}\n\nEnglish:\n{en}\n\n{tags}\n"
            if bn:
                body += f"\nবাংলা:\n{bn}\n\n{tags}\n"
            (pack / f"{name}.txt").write_text(body, "utf-8")
            words, _ = _words_for(j, {**c, "seg": c.get("seg")})
            if words:
                if c.get("cuts"):  # subtitles follow the jump-cut timeline
                    words = render.jump_words(words, c["start"], c["cuts"])
                    end = c["start"] + sum(b - a for a, b in c["cuts"])
                else:
                    end = c["end"]
                (pack / f"{name}.{fb_lang}.srt").write_text(_srt(words, c["start"], end), "utf-8")
            j.data["export"].update(done=i + 1); j.save()
        j.set(export={"state": "done", "count": len(clips), "path": f"{j.id}/{pack.name}", "warnings": warnings})
    except Exception as e:
        j.log(f"Export failed: {e}")
        j.set(export={"state": "error", "warnings": [str(e)[:200]]})
