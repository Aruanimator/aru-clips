"""Projects: the clipping pipeline, live-stream clipping, re-renders and the Facebook / Instagram export pack."""
import json, math, os, re, shutil, sys, time, threading, traceback, pathlib, uuid, subprocess
import numpy as np
from config import output_dir, load_settings, slug, FFMPEG, popen, run, UPLOADS
import ai, media, picker, render, signals, publish, taste, post as postmod

JOBS = {}
_pipe_lock = threading.Lock()   # one download/transcribe pipeline at a time
_render_lock = threading.Lock()  # one render at a time keeps the PC responsive


class Job:
    def __init__(self, jid, d, data=None):
        self.id, self.dir = jid, pathlib.Path(d)
        self.data = data or {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.pausing = False   # stop means "pause, keep everything for later" instead of "finish up"
        self.thread = None

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
        for c in data.get("clips", []):  # uploads cut off when the app closed can be published again
            for st in (c.get("published") or {}).values():
                if st.get("state") in ("queued", "uploading"):
                    st.clear(); st.update(state="error", error="Stopped when the app closed. Publish again to retry.")
        if data.get("status") in ("running", "queued") or (data.get("status") == "error" and str(data.get("error", "")).startswith("The app was closed")):
            data["status"] = "paused"; data["error"] = None
            data["stage"] = "Paused because the app was closed. Press Resume to carry on where it stopped."
        for c in data.get("clips", []):
            c.pop("rendering", None)
        j = JOBS[jid] = Job(jid, jf.parent, data)
        for c in data.get("clips", []):  # moments you marked that didn't finish before the app closed
            if c.get("manual") and not c.get("file") and (c.get("wall") or data.get("opts", {}).get("source_type") != "live"):
                c["rendering"] = True
                threading.Thread(target=_manual_run, args=(j, c), daemon=True).start()


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
    _start(j)
    return jid


class Paused(BaseException):
    """Raised at a safe point when the user pauses a project. BaseException so no 'except Exception' swallows it."""


def _check(j):
    if j.stop.is_set():
        raise Paused()


def _start(j):
    j.stop = threading.Event()
    j.pausing = False
    target = run_live if (j.get("opts") or {}).get("source_type") == "live" else run_job
    j.thread = threading.Thread(target=_guard, args=(j, target), daemon=True)
    j.thread.start()


def _busy(j):
    return j.thread is not None and j.thread.is_alive()


def pause(j):
    """Stops work at the next safe point and keeps everything, so Resume carries on from there."""
    if j.get("status") not in ("running", "queued") or not _busy(j):
        return False
    j.pausing = True
    j.stop.set()
    j.stage("Pausing…")
    return True


def resume(j):
    if _busy(j) or j.get("status") not in ("paused", "error"):
        return False
    j.set(status="queued", error=None, stage="Resuming…")
    j.log("Resumed")
    _start(j)
    return True


def _halt(j, wait=60):
    if _busy(j):
        j.pausing = True
        j.stop.set()
        j.thread.join(timeout=wait)


def restart(j):
    """Start over in the same project: clips, picks and the recording are cleared; a downloaded video is kept."""
    _halt(j)
    live = (j.get("opts") or {}).get("source_type") == "live"
    for p in list(j.dir.iterdir()):
        keep = p.name == "job.json" or (not live and p.name.startswith("source.") and (j.get("steps") or {}).get("source"))
        keep = keep or p.name in ("heatmap.json",) or (not live and p.name.startswith("chat"))
        if keep:
            continue
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass
    steps = {"source": True} if not live and (j.get("steps") or {}).get("source") else {}
    with j.lock:
        for k in ("plan", "live_state", "live", "_last_n", "lang", "_enc_logged", "chunked", "chunk_state", "dl"):
            j.data.pop(k, None)
        j.data.update(clips=[], steps=steps, progress=0, error=None, export={}, status="queued", stage="Starting over…")
        j.save()
    j.log("Started over")
    _start(j)


def delete(jid):
    j = get(jid)
    if not j:
        return False
    media.stop_chat(j.dir)  # a chat replay still downloading would keep writing into the folder
    _halt(j, 30)
    JOBS.pop(jid, None)
    for _ in range(30):  # Windows keeps files locked for a moment after ffmpeg exits
        shutil.rmtree(j.dir, ignore_errors=True)
        if not j.dir.exists():
            break
        time.sleep(0.5)
    return True


def next_n(j):
    with j.lock:
        n = max([c["n"] for c in j.data.get("clips", [])] + [int(j.data.get("_last_n", 0))]) + 1
        j.data["_last_n"] = n
        return n


def _guard(j, fn):
    try:
        fn(j)
    except Paused:
        j.set(status="paused", error=None, stage="Paused. Press Resume to carry on from here, or Start over.")
        j.log("Paused")
    except Exception as e:
        if j.stop.is_set() and j.pausing:
            j.set(status="paused", error=None, stage="Paused. Press Resume to carry on from here, or Start over.")
            return
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
    name = (c.get("seg") or c.get("chunk") or "transcript") + ".json"
    p = j.dir / "live" / name if c.get("seg") else j.dir / "chunks" / name if c.get("chunk") else j.dir / name
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
    if c.get("chunk"):
        return j.dir / "chunks" / (c["chunk"] + ".mp4")
    if c.get("seg"):
        mp4 = j.dir / "live" / (c["seg"] + ".mp4")
        return mp4 if mp4.exists() else j.dir / "live" / (c["seg"] + ".ts")
    return next((p for p in j.dir.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts")), None)


def render_one(j, c):
    src = _src_for(j, c)
    if not src or not src.exists():
        raise RuntimeError("The source video was deleted to free up space, so this clip can't be re-rendered"
                           if j.get("source_dropped") else "The source video for this clip is missing")
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
    o = j.get("opts") or {}
    if not _pipe_lock.acquire(blocking=False):
        # another project is working: download this one meanwhile, so it's ready when that one finishes
        if o.get("url") and not o.get("file") and not (j.get("steps") or {}).get("source") and not j.get("chunked") and not _go_chunked(j, o):
            _predownload(j, o)
        j.set(status="queued", stage="Downloaded. Waiting for the previous project to finish…" if (j.get("steps") or {}).get("source")
              else "Waiting for the previous project to finish…")
        while not _pipe_lock.acquire(timeout=1):
            _check(j)
    try:
        _run_job(j)
    finally:
        _pipe_lock.release()


def _predownload(j, o):
    j.stage("Downloading while the previous project finishes…", 0.01)
    src, info = media.download(o["url"], j.dir, j.log, lambda f: (_check(j), j.set(progress=round(0.01 + 0.29 * f, 3))),
                               want_chat=(o.get("source_type") or "video") == "stream")
    j.set(title=info.get("title") or j.get("title"), streamer=info.get("uploader") or info.get("channel") or j.get("streamer"))
    if info.get("heatmap"):
        (j.dir / "heatmap.json").write_text(json.dumps(info["heatmap"]), "utf-8")
    with j.lock:
        j.data.setdefault("steps", {})["source"] = True; j.save()
    j.log("Downloaded")


def _run_job(j):
    o = j.get("opts")
    j.set(status="running")
    mode = o.get("source_type") or "video"
    steps = j.data.setdefault("steps", {})
    done_src = next((p for p in j.dir.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts")
                     and not re.search(r"\.f\d+\.", p.name)), None)
    if not steps and done_src and (j.dir / "transcript.json").exists():  # a project from before pause/resume existed
        steps.update(source=True, transcript=True)
    # 1. source
    if steps.get("source") and done_src:
        src = done_src
        j.stage("Video ready", 0.3)
    elif o.get("file"):
        up = UPLOADS / o["file"]
        if not up.exists():
            raise RuntimeError("The uploaded file is missing. Drop it again.")
        dst = j.dir / ("source" + up.suffix.lower())
        shutil.move(str(up), dst)
        src = dst
        j.set(title=o.get("title") or up.stem)
        j.stage("Video ready", 0.3)
    elif j.get("chunked") or _go_chunked(j, o):
        return _run_chunked(j, o, mode)
    else:
        j.stage("Downloading the video…", 0.01)
        src, info = media.download(o["url"], j.dir, j.log, lambda f: (_check(j), j.set(progress=round(0.01 + 0.29 * f, 3))),
                                   want_chat=mode == "stream")
        if info.get("uploader") or info.get("channel"):
            j.set(streamer=info.get("uploader") or info.get("channel"))
        j.set(title=info.get("title") or j.get("title"))
        if info.get("heatmap"):
            (j.dir / "heatmap.json").write_text(json.dumps(info["heatmap"]), "utf-8")
            j.log("Got YouTube's most-replayed graph for this video")
        j.stage("Downloaded", 0.3)
    with j.lock:
        steps["source"] = True; j.save()
    _check(j)
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
    trf = j.dir / "transcript.json"
    if steps.get("transcript") and trf.exists():
        tr = json.loads(trf.read_text("utf-8"))
        if not wav.exists():
            media.extract_audio(src, wav)
        j.stage("Transcript ready (kept from before)", 0.6)
    else:
        tr = _transcribe(j, src, wav, total, o.get("language", "auto"), 0.3, 0.6)
        trf.write_text(json.dumps(tr, ensure_ascii=False), "utf-8")
    with j.lock:
        steps["transcript"] = True; j.save()
    j.set(lang=tr["language"])
    _check(j)
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
    if steps.get("picked") and j.get("plan") is not None:
        clips = j.get("plan")
        j.log(f"Using the {len(clips)} moments picked before the pause")
        return _render_plan(j, clips)
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
    for c in clips:
        c.update(base, n=next_n(j))
    with j.lock:
        j.data["plan"] = clips; steps["picked"] = True; j.save()
    _render_plan(j, clips)


CHUNK = 3600          # long videos are downloaded and clipped an hour at a time
CHUNK_FROM = 75 * 60  # ...when they're longer than this


def _go_chunked(j, o):
    if not o.get("url") or o.get("file"):
        return False
    info = media.probe_url(o["url"])
    d = float(info.get("duration") or 0)
    if d < CHUNK_FROM or info.get("kind") == "live":
        return False
    with j.lock:
        j.data["chunked"] = {"total": d}
        j.save()
    if info.get("title"):
        j.set(title=info["title"])
    if info.get("uploader"):
        j.set(streamer=info["uploader"])
    j.log(f"This video is {int(d // 3600)}h{int(d % 3600 // 60):02d}m long, so it's downloaded and clipped an hour at a time: "
          "the first clips come after the first hour is in.")
    return True


def _slice_heat(heat, a, b):
    out = []
    for h in heat or []:
        s0, e0 = float(h.get("start_time", 0)), float(h.get("end_time", 0))
        if e0 > a and s0 < b:
            out.append(dict(h, start_time=max(0.0, s0 - a), end_time=min(b, e0) - a))
    return out or None


def _run_chunked(j, o, mode):
    """Long videos: the whole video downloads at full speed (sound first), and each hour is cut out and clipped
    as soon as the download has passed it, while the rest keeps downloading."""
    total = float(j.get("chunked")["total"])
    cdir = j.dir / "chunks"; cdir.mkdir(exist_ok=True)
    plan = [(k, k * CHUNK, min(total, (k + 1) * CHUNK)) for k in range(int(math.ceil(total / CHUNK)))]
    if plan and plan[-1][2] - plan[-1][1] < 300 and len(plan) > 1:  # fold a short tail into the previous hour
        k, a, _ = plan[-2]; plan = plan[:-2] + [(k, a, total)]
    state = j.data.setdefault("chunk_state", {})  # name -> "downloaded" | "done"
    n_want = int(o.get("num_clips", 6))
    mn, mx = float(o.get("min_len", 20)), float(o.get("max_len", 60))
    extra, heat = {}, None
    if "twitch.tv" in o["url"] and signals.twitch_video_id(o["url"]):
        threading.Thread(target=lambda: extra.update(chat=signals.twitch_replay_chat(o["url"], total, j.log)), daemon=True).start()
    try:
        heat = media.ydl_run(lambda y: y.extract_info(o["url"], download=False), {"skip_download": True}).get("heatmap")
    except Exception:
        pass

    # ---- the full-speed download, watched part by part
    gen = j._gen = object()  # a Resume starts a new download; this one stops at its next progress update
    dl = {"files": {}, "src": None, "err": None, "done": False}
    full = next((p for p in j.dir.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm") and not re.search(r"\.f\d+\.", p.name)
                 and (j.get("steps") or {}).get("source")), None)

    def hook(d):
        if j.stop.is_set() or getattr(j, "_gen", None) is not gen:
            raise Paused()
        inf = d.get("info_dict") or {}
        fid = str(inf.get("format_id") or "x")
        vc, ac = inf.get("vcodec") or "", inf.get("acodec") or ""
        kind = "a" if vc in ("", "none") and ac not in ("", "none") else "v" if ac in ("", "none") and vc not in ("", "none") else "av"
        r = dl["files"].setdefault(fid, {"kind": kind})
        r.update(tmp=d.get("tmpfilename") or d.get("filename"), final=d.get("filename"), got=d.get("downloaded_bytes") or r.get("got", 0),
                 tot=d.get("total_bytes") or d.get("total_bytes_estimate") or r.get("tot", 0), finished=d.get("status") == "finished")

    def runner():
        try:
            src, info = media.download(o["url"], j.dir, j.log, lambda f: None, want_chat=mode == "stream" and "youtu" in o["url"],
                                       fmt=media.AUDIO_FIRST, on_hook=hook)
            dl["src"] = src
            if info.get("heatmap") and not heat:
                (j.dir / "heatmap.json").write_text(json.dumps(info["heatmap"]), "utf-8")
        except BaseException as e:
            dl["err"] = e
        finally:
            dl["done"] = True
    if full:
        dl.update(src=full, done=True)
    elif any(state.get(f"ch_{k:03d}") != "done" for k, _, _ in plan):
        j.log("Downloading the whole video at full speed (sound first); each hour is clipped as soon as it's in")
        threading.Thread(target=runner, daemon=True).start()

    def frac():
        v = next((r for r in dl["files"].values() if r["kind"] in ("v", "av")), None)
        if not v:
            return 0.0, None, None
        f = 1.0 if v["finished"] else (v["got"] / v["tot"] if v.get("tot") else 0.0)
        a = next((r for r in dl["files"].values() if r["kind"] == "a"), None)
        return f, v, a

    def inputs_for(b):
        """Files to cut hour ending at b from, once the download has passed it."""
        if dl["src"] and pathlib.Path(dl["src"]).exists():
            return [(dl["src"], "")]
        f, v, a = frac()
        if not v or f < min(1.0, b / total + 0.03):
            return None
        vp = v["final"] if v["finished"] and os.path.exists(v["final"]) else v["tmp"]
        if v["kind"] == "av":
            return [(vp, "")]
        if not a or not a["finished"] or not os.path.exists(a["final"]):
            return None
        return [(vp, ""), (a["final"], "")]

    base = {"crop_mode": o.get("crop_mode", "face"), "caption_style": o.get("caption_style", "bold"),
            "caption_position": o.get("caption_position", 0.7), "title_overlay": o.get("title_overlay", True),
            "format": o.get("format", "9:16"), "remove_silence": bool(o.get("remove_silence"))}
    done_n = sum(1 for v in state.values() if v == "done")
    for k, a, b in plan:
        name = f"ch_{k:03d}"
        if state.get(name) == "done":
            continue
        src = cdir / (name + ".mp4")
        p0 = 0.02 + 0.96 * done_n / len(plan)
        if not (state.get(name) == "downloaded" and src.exists() and _playable(src)):
            last = 0
            while True:  # wait for the download to pass this hour, then cut it out
                _check(j)
                if dl["err"]:
                    raise dl["err"]
                ins = inputs_for(b)
                if ins:
                    try:
                        got = media.cut_section(ins, a, b, src)
                        if got >= (b - a) * 0.97 or dl["src"]:
                            break
                    except Exception as e:
                        if dl["src"]:
                            raise
                        j.log(f"Hour {k + 1} isn't fully in yet, waiting a little longer ({str(e)[:80]})")
                    src.unlink(missing_ok=True)
                    time.sleep(15)
                    continue
                f, _, _ = frac()
                if time.time() - last > 4:
                    with j.lock:  # progress without filling the activity log
                        j.data["stage"] = f"Downloading… {int(f * 100)}% of the video (hour {k + 1} is ready at {min(100, int(100 * (b / total + 0.03)))}%)"
                        j.data["progress"] = round(p0, 4)
                    last = time.time()
                if dl["done"] and not dl["src"]:
                    raise RuntimeError("The download stopped before this hour")
                time.sleep(2)
            with j.lock:
                state[name] = "downloaded"; j.save()
            j.log(f"Hour {k + 1} of {len(plan)} is in ({_hms(a)}–{_hms(b)})")
        dur = media.ffprobe(src)["duration"] or (b - a)
        j.stage(f"Hour {k + 1} of {len(plan)}: transcribing…", p0)
        wav = cdir / (name + ".wav")
        trf = cdir / (name + ".json")
        if trf.exists():
            tr = json.loads(trf.read_text("utf-8"))
            if not wav.exists():
                media.extract_audio(src, wav)
        else:
            tr = _transcribe(j, src, wav, dur, o.get("language", "auto"), p0, p0 + 0.4 / len(plan))
            trf.write_text(json.dumps(tr, ensure_ascii=False), "utf-8")
        j.set(lang=tr["language"])
        _check(j)
        loud = media.loudness(wav)
        wav.unlink(missing_ok=True)
        if not extra.get("chat") and mode == "stream" and "youtu" in o["url"]:
            extra["chat"] = signals.yt_replay_chat(j.dir) or None
        chat = [(t - a, x) for t, x in (extra.get("chat") or []) if a <= t < a + dur]
        j.stage(f"Hour {k + 1} of {len(plan)}: finding the best moments…", p0 + 0.45 / len(plan))
        ok = None
        if o.get("crop_mode") == "split2":
            try:
                ok = render.split_timeline(src, dur, j.log)
            except Exception as e:
                j.log(f"Couldn't check the streamer's screen time ({str(e)[:120]})")
        want = max(2, int(round(n_want * dur / total + 0.49)))
        clips, eng = picker.pick_audience(tr, dur, want * 3 if ok else want, mn, mx, mode, o.get("prompt", ""), chat or None,
                                          _slice_heat(heat, a, a + dur), loud, j.log, lambda f: None,
                                          o.get("content", "general"), o.get("game", "any"), 6.0, None, *_pads(o))
        if ok:
            clips = _prefer_split(clips, ok, want, mn, j.log)
            clips = [picker.finish_sentence(c, tr["words"], tr["segments"], mn, mx, dur) for c in clips]
        j.log(f"Hour {k + 1}: picked {len(clips)} clips" + (f" with {eng}" if eng and eng != "heuristic" else ""))
        clips.sort(key=lambda c: -c["score"])
        for i, c in enumerate(clips):
            _check(j)
            c.update(base, n=next_n(j), chunk=name, offset=a)
            j.stage(f"Hour {k + 1} of {len(plan)}: rendering clip {i + 1} of {len(clips)}", p0 + (0.55 + 0.4 * i / max(1, len(clips))) / len(plan))
            with j.lock:
                j.data["clips"].append(c)
            try:
                render_one(j, c)
                auto_publish(j, c)
            except Exception as e:
                j.log(f"Clip {c['n']} failed: {str(e)[:300]}")
                with j.lock:
                    j.data["clips"].remove(c)
        with j.lock:
            state[name] = "done"; j.save()
        done_n += 1
    t_end = time.time() + 600
    while not dl["done"] and time.time() < t_end:  # let the download finish joining sound and picture
        _check(j); time.sleep(2)
    if dl["src"]:
        with j.lock:
            j.data.setdefault("steps", {})["source"] = True; j.save()
    n = len([c for c in j.data["clips"] if c.get("file")])
    j.set(status="done", stage=f"Done! {n} clips saved.", progress=1)


def _playable(f):
    try:
        return (media.ffprobe(f)["duration"] or 0) > 1
    except Exception:
        return False


def _hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _render_plan(j, clips):
    """Renders the picked moments that aren't rendered yet (all of them, or what's left after a pause)."""
    have = {c["n"] for c in j.data.get("clips", []) if c.get("file")}
    gone = {c["n"] for c in j.data.get("clips", []) if not c.get("manual")} - have  # half-rendered when paused: render again
    with j.lock:
        j.data["clips"] = [c for c in j.data["clips"] if c["n"] not in gone]
    todo = [c for c in clips if c["n"] not in have]
    for k, c in enumerate(todo):
        _check(j)
        if c["n"] in (j.data.get("_deleted") or []):
            continue
        c = dict(c)
        j.stage(f"Rendering clip {len(have) + k + 1} of {len(clips)}: {c['title'][:50]}", 0.7 + 0.3 * (len(have) + k) / len(clips))
        with j.lock:
            j.data["clips"].append(c)
        try:
            render_one(j, c)
            auto_publish(j, c)
        except Exception as e:
            j.log(f"Clip {c['n']} failed: {str(e)[:300]}")
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
    tr = ai.transcribe(wav, total, lang, j.log, lambda f: (_check(j), j.set(progress=round(p0 + 0.01 + (p1 - p0 - 0.01) * f, 3))))
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
    (ldir / "hls").mkdir(exist_ok=True)
    s = load_settings()
    st = j.get("live_state") or {}  # what an earlier session (before a pause) already did
    probe = media.probe_url(o["url"])
    m = re.search(r"(?:twitch\.tv|kick\.com)/([^/?#]+)", o["url"])
    j.set(streamer=probe.get("uploader") or (m.group(1) if m else j.get("streamer")))
    j.set(status="running", title="LIVE · " + (probe.get("title") or o["url"]),
          live={"recording": True, "elapsed": int(st.get("elapsed", 0)), "parts": len(st.get("done", []))})
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
        pipe = rec_state.get("pipe") or any(x in o["url"] for x in media.PIPE_SITES)
        args, maps = media.ffmpeg_inputs(inputs)
        if pipe:  # yt-dlp fetches the stream and hands it to ffmpeg, which only saves it
            ytp = popen(media.ytdlp_pipe_cmd(o["url"]), stdout=subprocess.PIPE, stderr=ylog)
            args, maps = ["-i", "pipe:0"], ["-map", "0:v:0?", "-map", "0:a:0?"]
        nums = [int(m.group(1)) for q in ldir.glob("seg_*") if (m := re.match(r"seg_(\d+)\.(ts|mp4)$", q.name))]
        start_no = max(nums) + 1 if nums else 0
        # -copyts keeps the stream's own clock, so separate video and audio feeds stay in sync
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-copyts"] + args + maps + [
            "-c", "copy", "-f", "segment"] + (
            ["-segment_times", ",".join(f"{first + k * chunk:.0f}" for k in range(400))] if first else ["-segment_time", str(chunk)]) + [
            "-segment_start_number", str(start_no), str(ldir / "seg_%03d.ts")]
        if pipe:
            rec_state["proc"] = popen(cmd, stdin=ytp.stdout, stderr=ylog)
            ytp.stdout.close()  # ffmpeg owns the pipe now
            rec_state["yt"] = ytp
        else:
            rec_state["proc"] = popen(cmd, stdin=subprocess.PIPE, stderr=ylog)
            rec_state["yt"] = None
        rec_state["pipe_on"] = pipe
        rec_state["started"] = rec_state["grew"] = time.time()
        rec_state["size"] = -1
        return True

    prev = {"proc": None, "file": None}

    def preview_tick():
        """The in-app player's feed: a separate ffmpeg follows the part being recorded and cuts it into short HLS
        pieces. It's kept apart from the recorder so the recording never depends on it."""
        segs = sorted(ldir.glob("seg_*.ts"))
        if not segs:
            return
        cur, pp = segs[-1], prev["proc"]
        if prev["file"] == cur.name:
            return  # following this part already (or it ended: a new part brings it back)
        if pp and pp.poll() is None:
            pp.terminate()
        pl = ldir / "hls" / "live.m3u8"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-follow", "1", "-rw_timeout", "20000000", "-i", str(cur),
               "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_list_size", "1800",
               "-hls_flags", "delete_segments+append_list+program_date_time+omit_endlist" + ("+discont_start" if pl.exists() else ""),
               "-hls_segment_filename", str(ldir / "hls" / "h_%06d.ts"), str(pl)]
        try:
            prev["proc"] = popen(cmd, stdin=subprocess.DEVNULL, stderr=ylog)
            prev["file"] = cur.name
        except Exception as e:
            ylog.write(("preview: " + str(e) + "\n").encode("utf-8", "ignore")); ylog.flush()

    def stall_check():
        """A connection can hang without ffmpeg quitting. If the recording stops growing for a minute, restart it."""
        p = rec_state.get("proc")
        if not p or p.poll() is not None:
            return
        segs = sorted(ldir.glob("seg_*.ts"))
        size = sum(q.stat().st_size for q in segs[-2:]) if segs else 0
        if size != rec_state.get("size"):
            rec_state["size"], rec_state["grew"] = size, time.time()
        elif time.time() - rec_state.get("grew", time.time()) > 60:
            j.log("The stream stopped sending video for a minute, reconnecting…")
            ylog.write(b"stalled: no data for 60 s, restarting\n"); ylog.flush()
            if not rec_state.get("pipe_on"):
                rec_state["pipe"] = True  # ffmpeg alone stalled on this stream: let yt-dlp fetch it from now on
                j.log("Switching to yt-dlp to fetch this stream")
            yt = rec_state.get("yt")
            if yt and yt.poll() is None:
                yt.kill()
            p.kill()

    def stop_recorder():
        p, yt = rec_state["proc"], rec_state.get("yt")
        if yt and yt.poll() is None:
            yt.kill()  # ffmpeg sees the end of the pipe and closes the last part cleanly
        if not p or p.poll() is not None:
            return
        try:
            if yt:
                p.wait(timeout=20)
            else:
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

    def keeper():  # own thread: processing a part takes minutes and mustn't freeze the player or the watchdog
        while not watch_stop.is_set():
            try:
                preview_tick()
                stall_check()
            except Exception as e:
                try:
                    ylog.write(("keeper: " + str(e) + "\n").encode("utf-8", "ignore")); ylog.flush()
                except Exception:
                    pass
            watch_stop.wait(2)
    threading.Thread(target=keeper, daemon=True).start()
    carry = list(st.get("carry", []))  # parts cut short by a dropped connection or a pause, joined with the rest of their batch
    resume_left = None
    if st:  # resuming: the part that was recording when it paused waits for the rest of its batch
        left_over = [x for x in sorted(q.stem for q in ldir.glob("seg_*.ts")) if x not in st.get("done", []) and x not in carry]
        if left_over:
            def _d(x):
                try:
                    return media.ffprobe(ldir / (x + ".ts"))["duration"] or 0
                except Exception:
                    return 0
            have = sum(_d(x) for x in carry + left_over[-1:])
            if have < chunk * 0.9:
                carry.append(left_over[-1])
                resume_left = max(20.0, chunk - have)
        j.log("Reconnecting to the stream to carry on recording")
    if not start_recorder(resume_left):
        watch_stop.set()
        if chat_reader: chat_reader.stop.set()
        ylog.close()
        err = (ldir / "recorder.log").read_text("utf-8", "ignore")[-400:]
        raise RuntimeError("Could not open this live stream. " + media.friendly_error(err.replace("resolve: ", "")))
    t0 = time.time() - float(st.get("elapsed", 0))
    done, offsets, durs = set(st.get("done", [])) | set(carry), dict(st.get("offsets", {})), dict(st.get("durs", {}))
    spike_ranges = []          # wall-clock ranges already covered by instant clips
    ai_lock = threading.Lock()  # parts and instant clips take turns on the AI
    mn, mx = float(o.get("min_len", 20)), float(o.get("max_len", 60))

    def clip_defaults(c):
        c.update(crop_mode=o.get("crop_mode", "face"), caption_style=o.get("caption_style", "bold"),
                 caption_position=o.get("caption_position", 0.7), title_overlay=o.get("title_overlay", True),
                 format=o.get("format", "9:16"), remove_silence=bool(o.get("remove_silence")))

    def add_clip(c, src_file):
        c["n"] = next_n(j)
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
                auto_publish(j, c)
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
        name = f"spike_{k + k0:03d}"
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
             "-af", f"aresample=async=1000,atrim=start={abs_a:.3f}:end={abs_b:.3f},asetpts=PTS-STARTPTS",
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

    k0 = len(list(ldir.glob("spike_*.mp4")))  # numbering carries on after a pause

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

    batches = [int(st.get("batches", 0))]

    def save_state():
        with j.lock:
            j.data["live_state"] = {"done": sorted(done - set(carry)), "offsets": offsets, "durs": durs, "batches": batches[0],
                                    "elapsed": int(time.time() - t0), "carry": carry}
            j.save()

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
        save_state()

    def status(rec_on):
        el = int(time.time() - t0)
        n = len(j.data.get("clips", []))
        j.set(live={"recording": rec_on, "elapsed": el, "parts": len(done)})
        save_state()
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
        if prev["proc"] and prev["proc"].poll() is None:
            time.sleep(1)  # let it pick up the last seconds
            prev["proc"].terminate()
        rec_state["end"] = time.time()
        ylog.close()
        watch_stop.set()
        if chat_reader:
            chat_reader.stop.set()
    if j.pausing:  # keep the unfinished part and everything not processed yet for Resume
        cut = sorted(q.stem for q in ldir.glob("seg_*.ts"))
        if cut and cut[-1] not in done:
            carry.append(cut[-1]); done.add(cut[-1])
        save_state()
        j.set(live={"recording": False, "elapsed": int(time.time() - t0), "parts": len(done)})
        raise Paused()
    j.set(live={"recording": False, "elapsed": int(time.time() - t0), "parts": len(done)})
    j.stage("Stopping. Finishing the last clips…")
    pl = ldir / "hls" / "live.m3u8"
    if pl.exists() and "#EXT-X-ENDLIST" not in pl.read_text("utf-8", "ignore"):
        with open(pl, "a", encoding="utf-8") as f:
            f.write("#EXT-X-ENDLIST\n")  # the player treats the finished recording as a normal video
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


# ======================================================================= publishing
def clip_text(j, c):
    words, _ = _words_for(j, c, edited=False)
    return " ".join(w["w"] for w in words if c["start"] <= w["s"] <= c["end"])


def post_for(j, c):
    """What gets posted: the text edited in the Publish box, or the template built for this clip."""
    try:
        built = postmod.build(j, c, clip_text(j, c))
    except Exception:
        built = postmod.build(j, c, "")
    p = c.get("post") or {}
    if p.get("title"):
        built["title"] = p["title"]
    if p.get("description"):
        built["description"] = p["description"]
    if p.get("tags"):
        built["tags"] = p["tags"]
    built["hashtags"] = re.findall(r"#\w+", built["description"]) or built["hashtags"]
    return built


def set_post_text(j, n, meta):
    """Saves the title, description and tags used when posting this clip."""
    c = j.clip(int(n))
    if not c:
        return
    tags = meta.get("tags", "")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    with j.lock:
        c["post"] = {"title": str(meta.get("title", "")).strip()[:100], "description": str(meta.get("description", "")).strip()[:4900],
                     "tags": tags[:40]}
        j.save()


def publish_clips(j, ns, platforms, why=None, when=None, force_now=False):
    """Queues rendered clips for upload (or schedules them for `when`, a Unix time).
    Returns how many were queued; why (a list) collects reasons for skipped ones."""
    when = float(when) if when else None
    if when and when <= time.time() + 30:
        when = None  # "later" that's already now
    platforms = [p for p in platforms if p in publish.PLATFORMS]
    if not platforms:
        raise ValueError("Pick at least one platform")
    gaming = (j.get("opts") or {}).get("content") == "gaming"
    why = why if why is not None else []
    k = 0
    for n in ns:
        c = j.clip(int(n))
        if not c or not c.get("file") or c.get("pending"):
            why.append(f"clip {n} isn't rendered yet")
            continue
        pub = c.get("published") or {}
        todo = [p for p in platforms if pub.get(p, {}).get("state") not in ("done", "uploading", "queued")]
        for p in platforms:
            st = pub.get(p, {}).get("state")
            if st == "done":
                why.append(f"clip {n} is already on {publish.NAMES[p]}")
            elif st in ("uploading", "queued"):
                why.append(f"clip {n} is already {'uploading' if st == 'uploading' else 'waiting'} for {publish.NAMES[p]}")
        if not todo:
            continue
        slot_when = when
        gap = int(publish.public()["auto"].get("space_min", 30)) * 60
        if not when and gap > 0 and not force_now:  # keep posts on each platform spaced out
            slots = [publish.next_slot(p, gap) for p in todo]
            if max(slots) > time.time() + 60:
                slot_when = max(slots)
                why.append(f"clip {n} is scheduled for later to keep posts {gap // 60} min apart")
        for p in todo:
            publish.take_slot(p, slot_when or time.time())
        if slot_when:  # the scheduler picks it up at that time
            when_ = slot_when
            with j.lock:
                pub = c.setdefault("published", {})
                for p in todo:
                    pub[p] = {"state": "scheduled", "when": int(when_)}
                j.save()
            k += 1
            continue
        with j.lock:
            pub = c.setdefault("published", {})
            for p in todo:
                pub[p] = {"state": "queued"}
            j.save()

        def upd(p, st, c=c):
            with j.lock:
                c.setdefault("published", {})[p] = st
                j.save()
        meta = dict(post_for(j, c), gaming=gaming or bool((j.get("opts") or {}).get("game") in postmod.GAME_NAMES))
        publish.enqueue({"path": str(output_dir() / c["file"]), "meta": meta, "platforms": todo, "on_update": upd, "log": j.log})
        k += 1
    return k


def unschedule(j, n, platform):
    c = j.clip(int(n))
    if not c:
        return False
    with j.lock:
        pub = c.get("published") or {}
        if (pub.get(platform) or {}).get("state") == "scheduled":
            pub.pop(platform, None); j.save()
            return True
    return False


_sched = {"t": None}


def scheduler_tick(now=None):
    """Publishes every scheduled post whose time has come. Returns how many clips it sent."""
    load_all()
    now = now or time.time()
    sent = 0
    for j in list(JOBS.values()):
        for c in list(j.data.get("clips", [])):
            due = [p for p, st in (c.get("published") or {}).items() if st.get("state") == "scheduled" and st.get("when", 0) <= now]
            if due:
                with j.lock:
                    for p in due:
                        c["published"].pop(p, None)
                j.log(f"Scheduled time reached: publishing clip {c['n']} to " + ", ".join(publish.NAMES[p] for p in due))
                publish_clips(j, [c["n"]], due, force_now=True)
                sent += 1
    return sent


def start_scheduler():
    """Posts scheduled clips when their time comes (if the app was closed then, as soon as it opens again)."""
    if _sched["t"] and _sched["t"].is_alive():
        return

    def loop():
        while True:
            try:
                scheduler_tick()
            except Exception as e:
                print("scheduler:", e)
            time.sleep(20)
    _sched["t"] = threading.Thread(target=loop, daemon=True)
    _sched["t"].start()


def auto_publish(j, c):
    """Publishes a freshly rendered clip by itself when auto-publish is on and the clip clears the score bar."""
    try:
        a = publish.public()["auto"]
        if not a.get("on") or not a.get("platforms") or c.get("pending") or not c.get("file"):
            return
        if int(c.get("score") or 0) < int(a.get("min_score", 80)) and not c.get("manual"):
            return
        ready = [p for p in a["platforms"] if publish.acct(p).get("access_token")]
        if ready:
            j.log(f"Clip {c['n']} scored {c.get('score')}: auto-publishing to " + ", ".join(publish.NAMES[p] for p in ready))
            publish_clips(j, [c["n"]], ready)
    except Exception as e:
        j.log(f"Auto-publish failed: {str(e)[:200]}")


def rate(j, n, stars):
    c = j.clip(n)
    if not c:
        raise KeyError("clip")
    stars = max(0, min(5, int(stars)))
    with j.lock:
        if stars:
            c["stars"] = stars
        else:
            c.pop("stars", None)
        j.save()
    words, _ = _words_for(j, c, edited=False)
    text = " ".join(w["w"] for w in words if c["start"] <= w["s"] <= c["end"])
    taste.record(f"{j.id}:{n}", stars, c, text, (j.get("opts") or {}).get("content", "general"))
    return taste.summary()


def set_stay(j, n, pct):
    """'Stayed to watch' % from YouTube Studio, about an hour after posting. Feeds the ratings: 60%+ is a hit."""
    c = j.clip(int(n))
    if not c:
        raise KeyError("clip")
    pct = max(0.0, min(100.0, float(pct)))
    stars = 5 if pct >= 60 else 4 if pct >= 50 else 3 if pct >= 40 else 2 if pct >= 30 else 1
    with j.lock:
        c["stay"] = pct
        j.save()
    rate(j, n, stars)
    return stars


def board(limit=400):
    """Clips from every project on one board, waiting ones first."""
    load_all()
    out = []
    for j in JOBS.values():
        for c in j.data.get("clips", []):
            if c.get("file") or c.get("pending"):
                out.append(dict(c, jid=j.id, project=j.data.get("title", ""), project_status=j.data.get("status")))
    out.sort(key=lambda c: (not c.get("pending"), -(c.get("rendered_at") or 0), -(c.get("score") or 0)))
    return out[:limit]


# ======================================================================= manual clips (picked in the in-app player)
def _hls_index(ldir):
    """[(file, wall_start, duration)] of the live preview pieces."""
    import datetime
    pl = ldir / "hls" / "live.m3u8"
    if not pl.exists():
        return []
    out, pdt, dur = [], None, None
    for line in pl.read_text("utf-8", "ignore").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                pdt = datetime.datetime.fromisoformat(line.split(":", 1)[1]).timestamp()
            except ValueError:
                pdt = None
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0] or 0)
        elif line and not line.startswith("#"):
            if pdt is not None and dur and (ldir / "hls" / line).exists():
                out.append((line, pdt, dur))
            pdt, dur = (pdt + dur if pdt is not None and dur else None), None
    return out


def _hls_cut(ldir, wa, wb, out):
    """Cuts wall-clock wa..wb out of the live preview pieces into an mp4 (picture and sound trimmed together)."""
    idx = _hls_index(ldir)
    if idx:
        edge = idx[-1][1] + idx[-1][2]
        if 0 < wb - edge < 20:  # the newest seconds aren't written yet: keep the length, end at the live edge
            wa, wb = wa - (wb - edge), edge
    use = [x for x in idx if x[1] + x[2] > wa and x[1] < wb]
    if not use:
        raise RuntimeError("That part of the stream isn't in the recording (the player keeps about the last 2 hours)")
    rel = max(0.0, wa - use[0][1])
    dur = min(wb, use[-1][1] + use[-1][2]) - max(wa, use[0][1])
    if dur < 2:
        raise RuntimeError("Pick at least 2 seconds")
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file 'hls/{x[0]}'\n" for x in use), "utf-8")
    raw = out.with_suffix(".cut.mp4")
    run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-ss", f"{rel:.3f}", "-t", f"{dur:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?", "-af", "aresample=async=1000:first_pts=0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(raw)], cwd=str(ldir))
    lst.unlink(missing_ok=True)
    if not raw.exists() or raw.stat().st_size < 5000:
        raise RuntimeError("Couldn't cut that part of the stream")
    os.replace(raw, out)  # already re-encoded from 0 with sound and picture together
    return out


def _transcribe_range(j, a, b, c=None):
    """Words for a stretch of the source video (times in source seconds)."""
    src = _src_for(j, c or {})
    wav = j.dir / f"range_{int(a)}_{int(b)}.wav"
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(src), "-vn",
         "-af", "aresample=async=1000:first_pts=0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    try:
        tr = ai.transcribe(wav, b - a, (j.get("opts") or {}).get("language", "auto"), j.log)
    finally:
        wav.unlink(missing_ok=True)
    return [dict(w, s=round(w["s"] + a, 3), e=round(w["e"] + a, 3)) for w in tr.get("words", [])]


def live_window(j):
    """Wall-clock range the live player can clip from, for the UI."""
    idx = _hls_index(j.dir / "live")
    return {"from": idx[0][1], "to": idx[-1][1] + idx[-1][2]} if idx else None


def manual_clip(j, body):
    """A moment the user marked in the player. Rendered straight away (no approval needed: they picked it)."""
    o = j.get("opts") or {}
    live = o.get("source_type") == "live"
    n = next_n(j)
    c = {"n": n, "manual": True, "picked_by": "you (marked in the player)", "reason": "You marked this moment.",
         "crop_mode": o.get("crop_mode", "face"), "caption_style": o.get("caption_style", "bold"),
         "caption_position": o.get("caption_position", 0.7), "title_overlay": o.get("title_overlay", True),
         "format": o.get("format", "9:16"), "remove_silence": bool(o.get("remove_silence")),
         "title": "Your clip", "score": 0, "caption": "", "hashtags": [], "rendering": True, "start": 0, "end": 1}
    if live:
        wa, wb = float(body["wall_start"]), float(body["wall_end"])
        c["wall"] = [wa, wb]
    else:
        a, b = float(body["start"]), float(body["end"])
        if b - a < 2:
            raise ValueError("Pick at least 2 seconds")
        c.update(start=round(a, 2), end=round(b, 2), duration=round(b - a, 1))
        if body.get("chunk"):  # long video, clipped an hour at a time: times are inside that hour
            ch = re.sub(r"[^\w]", "", str(body["chunk"]))
            if not (j.dir / "chunks" / (ch + ".mp4")).exists():
                raise ValueError("That hour isn't downloaded yet")
            c.update(chunk=ch, offset=int(ch.split("_")[-1]) * CHUNK)
    with j.lock:
        j.data["clips"].append(c); j.save()

    threading.Thread(target=_manual_run, args=(j, c), daemon=True).start()
    return n


def _manual_run(j, c):
    """Cuts, titles and renders a moment the user marked. Started again on launch if the app closed midway."""
    o = j.get("opts") or {}
    live = o.get("source_type") == "live"
    n = c["n"]
    try:
        if live:
            ldir = j.dir / "live"
            name = f"manual_{n:03d}"
            p = _hls_cut(ldir, c["wall"][0], c["wall"][1], ldir / (name + ".mp4"))
            d = media.ffprobe(p)["duration"]
            wav = ldir / (name + ".wav")
            media.extract_audio(p, wav)
            tr = ai.transcribe(wav, d, o.get("language", "auto"), j.log)
            wav.unlink(missing_ok=True)
            (ldir / (name + ".json")).write_text(json.dumps(tr, ensure_ascii=False), "utf-8")
            c.update(seg=name, start=0.0, end=round(d, 2), duration=round(d, 1))
            words = tr.get("words", [])
        else:
            # marked while the video was still downloading: wait for the download only (not the whole transcript)
            while not c.get("chunk") and not (j.get("steps") or {}).get("source"):
                if j.get("status") in ("error", "done") or JOBS.get(j.id) is None:
                    raise RuntimeError("the video didn't finish downloading")
                with j.lock:
                    c["stage"] = "Waiting for the download to finish"
                time.sleep(3)
            trf = j.dir / "chunks" / (c["chunk"] + ".json") if c.get("chunk") else j.dir / "transcript.json"
            if trf.exists():
                words, _ = _words_for(j, c)
            else:  # the full transcript isn't done yet: transcribe just this clip
                with j.lock:
                    c["stage"] = "Transcribing your clip"
                words = _transcribe_range(j, c["start"], c["end"], c)
                with j.lock:
                    c["words"] = words  # its captions
            c.pop("stage", None)
        text = " ".join(w["w"] for w in words if c["start"] - 0.2 <= w["s"] <= c["end"])
        c.update(picker.describe(text, j.get("lang", "en"), o.get("content", "general"), o.get("game", "any"), j.log))
        c["picked_by"] = "you (marked in the player)"
        j.log(f"Rendering your clip {n}: {c['title'][:60]}")
        render_one(j, c)
        auto_publish(j, c)
    except Exception as e:
        j.log(f"Your clip {n} failed: {str(e)[:250]}")
        with j.lock:
            j.data["clips"] = [x for x in j.data["clips"] if x is not c]
    finally:
        with j.lock:
            c.pop("rendering", None); j.save()


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
    auto_publish(j, c)


def reject(j, n):
    with j.lock:
        j.data["clips"] = [c for c in j.data["clips"] if c["n"] != n]
        j.data["_deleted"] = sorted(set(j.data.get("_deleted") or []) | {n})
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
        j.data["_deleted"] = sorted(set(j.data.get("_deleted") or []) | ns)
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
