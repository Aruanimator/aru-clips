"""Aru Clips local server. Run: python server.py  (opens http://127.0.0.1:8765)"""
import os, sys, re, json, shutil, time, threading, webbrowser, subprocess, pathlib, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # use Windows' own certificate store, so antivirus or company networks that inspect HTTPS don't break downloads
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
import requests
from fastapi import FastAPI, UploadFile, File, HTTPException, Body, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from config import (APP, ASSETS, DATA, UPLOADS, USER_FONTS, USER_SFX, FILLERS, FFMPEG, IS_WIN, load_settings, save_settings,
                    output_dir, run, slug)
import ai, media, jobs, publish, taste, watch
from render import WATERMARKS
WATERMARKS.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("ARU_PORT", "8765"))
app = FastAPI(title="Aru Clips")

CATALOG = [  # free Google Fonts, downloaded on demand
    ("bebas", "Bebas Neue", "Impact", "ofl/bebasneue/BebasNeue-Regular.ttf"),
    ("anton", "Anton", "Impact", "ofl/anton/Anton-Regular.ttf"),
    ("archivo", "Archivo Black", "Bold", "ofl/archivoblack/ArchivoBlack-Regular.ttf"),
    ("poppins", "Poppins ExtraBold", "Clean", "ofl/poppins/Poppins-ExtraBold.ttf"),
    ("bangers", "Bangers", "Comic", "ofl/bangers/Bangers-Regular.ttf"),
    ("luckiest", "Luckiest Guy", "Comic", "apache/luckiestguy/LuckiestGuy-Regular.ttf"),
    ("marker", "Permanent Marker", "Handwritten", "apache/permanentmarker/PermanentMarker-Regular.ttf"),
    ("nanumpen", "Nanum Pen Script", "Handwritten", "ofl/nanumpenscript/NanumPenScript-Regular.ttf"),
    ("bungee", "Bungee", "Display", "ofl/bungee/Bungee-Regular.ttf"),
    ("righteous", "Righteous", "Display", "ofl/righteous/Righteous-Regular.ttf"),
    ("russo", "Russo One", "Tech", "ofl/russoone/RussoOne-Regular.ttf"),
    ("blackops", "Black Ops One", "Gaming", "ofl/blackopsone/BlackOpsOne-Regular.ttf"),
    ("hindsiliguri", "Hind Siliguri Bold", "Bangla", "ofl/hindsiliguri/HindSiliguri-Bold.ttf"),
    ("tirobangla", "Tiro Bangla", "Bangla", "ofl/tirobangla/TiroBangla-Regular.ttf"),
]
GF = "https://raw.githubusercontent.com/google/fonts/main/"
SFX_META = {"pop": ("Pop", "UI"), "click": ("Click", "UI"), "mouse": ("Mouse click", "UI"), "tick": ("Number tick", "UI"),
            "key": ("Key tap", "UI"), "shine": ("Shine", "UI"), "ding": ("Ding", "UI"), "swoosh": ("Soft swoosh", "Transition"),
            "riser": ("Riser", "Transition"), "boom": ("Bass hit", "Impact"), "thud": ("Soft thud", "Impact")}


def safe_name(name):
    base = pathlib.Path(name or "file").name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .") or "file"
    return base


def within(root, rel):
    p = (pathlib.Path(root) / rel).resolve()
    if not str(p).startswith(str(pathlib.Path(root).resolve())):
        raise HTTPException(400, "Bad path")
    return p


# ======================================================================= page + static
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((APP / "static" / "index.html").read_text("utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/brand/logo.png")
@app.get("/logo.png")
def logo():
    return FileResponse(ASSETS / "brand" / "logo.png", headers={"Cache-Control": "no-cache"})  # logo changes with brand updates


@app.get("/ui/{name}")
def ui_asset(name: str):
    p = APP / "static" / "ui" / safe_name(name)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


@app.get("/media/{path:path}")
def media_file(path: str):
    p = within(output_dir(), path)
    if not p.is_file():
        raise HTTPException(404)
    if p.suffix == ".m3u8":  # the live player's playlist changes every few seconds
        return FileResponse(p, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    return FileResponse(p)


@app.get("/fontfile/{name}")
def font_file(name: str):
    for d in (ASSETS / "fonts", USER_FONTS):
        p = d / safe_name(name)
        if p.exists():
            return FileResponse(p)
    raise HTTPException(404)


@app.get("/sfxfile/{sid:path}")
def sfx_file(sid: str):
    from render import sfx_path
    p = sfx_path(sid)
    if not p:
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/fillerthumb/{name}")
def filler_thumb(name: str):
    p = FILLERS / ".thumbs" / (safe_name(name) + ".jpg")
    if not p.exists():
        src = FILLERS / safe_name(name)
        if not src.exists():
            raise HTTPException(404)
        p.parent.mkdir(exist_ok=True)
        media.thumb(src, 2, p, 320)
    return FileResponse(p)


# ======================================================================= settings + health
def masked(k):
    return ("••••••••" + k[-4:]) if k else ""


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    s["groq_api_key"] = masked(s.get("groq_api_key", ""))
    s["gemini_api_key"] = masked(s.get("gemini_api_key", ""))
    s["backup_key"] = masked(s.get("backup_key", ""))
    if not s.get("backup_ai") and s.get("gemini_api_key"):
        s["backup_ai"], s["backup_key"] = "gemini", s["gemini_api_key"]
    s["groq_extra_keys"] = "\n".join(masked(k) for k in re.split(r"[\s,;]+", s.get("groq_extra_keys", "")) if k)
    return s


@app.post("/api/settings")
def post_settings(body: dict = Body(...)):
    allowed = {"engine", "groq_api_key", "groq_llm_model", "groq_whisper_model", "ollama_model", "ollama_url", "output_dir",
               "cookies_browser", "local_whisper_model", "groq_extra_keys", "gemini_api_key", "gemini_model", "video_encoder",
               "backup_ai", "backup_key", "backup_model", "learn_taste"}
    patch = {k: v for k, v in body.items() if k in allowed}
    for k in ("groq_api_key", "gemini_api_key", "backup_key"):
        if str(patch.get(k, "")).startswith("••"):
            patch.pop(k)
    if "groq_extra_keys" in patch:  # keep saved keys shown masked, add new ones
        old = {k[-4:]: k for k in re.split(r"[\s,;]+", load_settings().get("groq_extra_keys", "")) if k}
        keep = []
        for k in re.split(r"[\s,;]+", patch["groq_extra_keys"] or ""):
            if not k:
                continue
            keep.append(old.get(k[-4:], "") if k.startswith("••") else k)
        patch["groq_extra_keys"] = "\n".join(k for k in keep if k)
    if patch.get("output_dir"):
        try:
            pathlib.Path(patch["output_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception:
            raise HTTPException(400, "Can't use that folder for saving clips")
    else:
        patch.pop("output_dir", None)
    key = patch.get("groq_api_key")
    if key:
        try:
            r = requests.get(ai.GROQ + "/models", headers={"Authorization": "Bearer " + key}, timeout=8)
            if r.status_code == 401:
                raise HTTPException(400, "That Groq key was rejected. Copy it again from console.groq.com/keys")
        except requests.RequestException:
            pass
    save_settings(patch)
    import render
    render._enc_cache.clear()
    jobs.JOBS.clear()
    return {"ok": True}


@app.get("/api/health")
def health():
    s = load_settings()
    up, model = ai.ollama_status(s)
    import render
    b = ai.backup_ai(s)
    return {"ffmpeg": bool(FFMPEG), "groq_key": ai.has_groq(s), "groq_keys": len(ai.groq_keys(s)), "gemini": ai.has_gemini(s),
            "backup": ai.BACKUPS[b[0]]["name"] if b else "",
            "ollama": up, "ollama_model": model, "local_whisper": ai.local_whisper_name(),
            "encoder": render.pick_encoder()[1] if FFMPEG else "CPU"}


@app.post("/api/backup/test")
def backup_test(body: dict = Body(...)):
    """Sends one tiny request to the backup AI so Settings can show whether the key works."""
    kind = (body.get("kind") or "").lower()
    key = (body.get("key") or "").strip()
    if key.startswith("••"):
        key = load_settings().get("backup_key") or load_settings().get("gemini_api_key", "")
    if kind not in ai.BACKUPS or not key:
        raise HTTPException(400, "Pick a backup AI and paste its key first")
    msgs = [{"role": "user", "content": 'Reply with JSON only: {"ok": true}'}]
    try:
        if kind == "gemini":
            txt, model = ai._gemini_chat(msgs, key, body.get("model") or None)
        else:
            txt, model = ai._oai_chat(kind, msgs, key, body.get("model") or None)
        ai.parse_json(txt)
        return {"ok": True, "model": model}
    except ai.RateLimited as e:
        return {"ok": False, "error": "The key works but it's on its rate limit right now. " + str(e)[:150]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ======================================================================= sources
@app.post("/api/probe")
def probe(body: dict = Body(...)):
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Paste a full link that starts with https://")
    return media.probe_url(url)


@app.get("/api/streams")
def streams(q: str = "gta 6", game: str = "Grand Theft Auto VI"):
    import signals
    return signals.find_live_streams(q, game)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = safe_name(file.filename)
    dst = UPLOADS / f"{uuid.uuid4().hex[:8]}_{name}"
    with open(dst, "wb") as f:
        while chunk := await file.read(8 << 20):
            f.write(chunk)
    return {"file": dst.name, "title": pathlib.Path(name).stem}


# ======================================================================= jobs
@app.post("/api/jobs")
def create_job(body: dict = Body(...)):
    if not FFMPEG:
        raise HTTPException(400, "ffmpeg is missing. Run Install.bat again.")
    if not body.get("url") and not body.get("file"):
        raise HTTPException(400, "Paste a link or drop a video first")
    save_settings({"num_clips": body.get("num_clips", 6), "caption_style": body.get("caption_style", "bold"),
                   "remove_silence": bool(body.get("remove_silence")),
                   "content": body.get("content", "general"), "game": body.get("game", "any"),
                   "pre_roll": body.get("pre_roll", 3), "post_roll": body.get("post_roll", 4),
                   "crop_mode": body.get("crop_mode", "face"),
                   "look": {"look": body.get("look") or {}, "format": body.get("format"), "sfx": body.get("sfx") or {},
                            "crop_mode": body.get("crop_mode"), "layout": body.get("layout") or {},
                            "caption_position": body.get("caption_position", 0.7)}})
    return {"id": jobs.create(body)}


@app.get("/api/jobs")
def list_jobs():
    return jobs.listing()


def job_or_404(jid):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "Project not found")
    return j


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return job_or_404(jid).public()


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: str):
    job_or_404(jid).stop.set()
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{n}/approve")
def approve(jid: str, n: int):
    try:
        jobs.approve(job_or_404(jid), n)
    except KeyError:
        raise HTTPException(404, "Clip not found")
    except Exception as e:
        raise HTTPException(500, f"Render failed: {str(e)[:200]}")
    return {"ok": True}


@app.get("/api/jobs/{jid}/clips/{n}/edit")
def edit_info(jid: str, n: int, original: int = 0):
    try:
        return jobs.edit_info(job_or_404(jid), n, bool(original))
    except KeyError:
        raise HTTPException(404, "Clip not found")


@app.post("/api/jobs/{jid}/clips/{n}/reject")
def reject(jid: str, n: int):
    jobs.reject(job_or_404(jid), n)
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{n}/rerender")
def rerender(jid: str, n: int, body: dict = Body(...)):
    try:
        jobs.rerender(job_or_404(jid), n, body)
    except KeyError:
        raise HTTPException(404, "Clip not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Render failed: {str(e)[:200]}")
    return {"ok": True}


@app.post("/api/jobs/{jid}/export")
def export(jid: str, body: dict = Body(default={})):
    j = job_or_404(jid)
    ns = body.get("ns") if isinstance(body, dict) else None
    ns = {int(n) for n in ns} if ns else None
    if not any(c.get("file") and (ns is None or c["n"] in ns) for c in j.data.get("clips", [])):
        raise HTTPException(400, "Render at least one clip first" if ns is None else "None of the selected clips is rendered yet")
    jobs.export_pack(j, ns)
    return {"state": "running"}


@app.post("/api/jobs/{jid}/clips/delete")
def delete_clips(jid: str, body: dict = Body(...)):
    return {"deleted": jobs.delete_clips(job_or_404(jid), body.get("ns") or [])}


# ---- project lifecycle
@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    job_or_404(jid)
    threading.Thread(target=jobs.delete, args=(jid,), daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_jobs(body: dict = Body(default={})):
    """Deletes several projects: the ids given, or every project that isn't running."""
    ids = body.get("ids") or [x["id"] for x in jobs.listing() if x["status"] not in ("running", "queued")]
    for jid in ids:
        threading.Thread(target=jobs.delete, args=(jid,), daemon=True).start()
    return {"deleted": len(ids)}


@app.post("/api/jobs/{jid}/pause")
def pause_job(jid: str):
    if not jobs.pause(job_or_404(jid)):
        raise HTTPException(400, "This project isn't running")
    return {"ok": True}


@app.post("/api/jobs/{jid}/resume")
def resume_job(jid: str):
    if not jobs.resume(job_or_404(jid)):
        raise HTTPException(400, "This project can't be resumed right now")
    return {"ok": True}


@app.post("/api/jobs/{jid}/restart")
def restart_job(jid: str):
    j = job_or_404(jid)
    threading.Thread(target=jobs.restart, args=(j,), daemon=True).start()
    return {"ok": True}


# ---- in-app player and manual clips
@app.get("/api/jobs/{jid}/player")
def player(jid: str):
    j = job_or_404(jid)
    o = j.get("opts") or {}
    clips = [{"n": c["n"], "start": c["start"], "end": c["end"], "seg": c.get("seg"), "title": c.get("title", ""),
              "pending": bool(c.get("pending")), "manual": bool(c.get("manual"))} for c in j.data.get("clips", [])]
    if o.get("source_type") == "live":
        pl = j.dir / "live" / "hls" / "live.m3u8"
        return {"kind": "live", "url": f"/media/{j.id}/live/hls/live.m3u8" if pl.exists() else "",
                "window": jobs.live_window(j), "recording": bool((j.get("live") or {}).get("recording")), "clips": clips}
    src = jobs._src_for(j, {})
    ok = src and src.exists() and (j.get("steps") or {}).get("source") or (src and j.get("status") == "done")
    return {"kind": "video", "url": f"/media/{j.id}/{src.name}" if ok else "", "clips": clips}


@app.post("/api/jobs/{jid}/manual")
def manual(jid: str, body: dict = Body(...)):
    try:
        return {"n": jobs.manual_clip(job_or_404(jid), body)}
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e) or "Mark a start and an end first")


# ---- ratings
@app.post("/api/jobs/{jid}/clips/{n}/rate")
def rate(jid: str, n: int, body: dict = Body(...)):
    try:
        return jobs.rate(job_or_404(jid), n, body.get("stars", 0))
    except KeyError:
        raise HTTPException(404, "Clip not found")


@app.get("/api/taste")
def taste_info():
    return taste.summary()


@app.post("/api/taste/reset")
def taste_reset():
    taste.reset()
    return taste.summary()


@app.get("/api/board")
def board():
    return jobs.board()


# ---- streamer watchlist
@app.get("/api/watch")
def watch_list():
    return watch.items()


@app.post("/api/watch")
def watch_add(body: dict = Body(...)):
    if not str(body.get("target", "")).strip():
        raise HTTPException(400, "Paste a streamer's link or name")
    try:
        return watch.add(body["target"], body.get("opts") or {}, body.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/watch/{iid}")
def watch_edit(iid: str, body: dict = Body(...)):
    watch.edit(iid, **body)
    return {"ok": True}


@app.delete("/api/watch/{iid}")
def watch_del(iid: str):
    watch.remove(iid)
    return {"ok": True}


# ---- custom watermarks
@app.get("/api/watermarks")
def wm_list():
    return [p.name for p in sorted(WATERMARKS.glob("*")) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]


@app.post("/api/watermarks")
async def wm_add(file: UploadFile = File(...)):
    name = safe_name(file.filename)
    if pathlib.Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "Use a PNG (transparent works best), JPG or WEBP image")
    (WATERMARKS / name).write_bytes(await file.read())
    return {"name": name}


@app.get("/wmfile/{name}")
def wm_file(name: str):
    p = WATERMARKS / safe_name(name)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.delete("/api/watermarks/{name}")
def wm_del(name: str):
    (WATERMARKS / safe_name(name)).unlink(missing_ok=True)
    return {"ok": True}


# ---- publishing
@app.get("/api/publish")
def pub_info(request: Request):
    return dict(publish.public(), redirect=str(request.base_url).rstrip("/") + "/oauth/callback")


@app.post("/api/publish/{platform}/app")
def pub_app(platform: str, body: dict = Body(...)):
    if platform not in publish.PLATFORMS:
        raise HTTPException(404)
    kw = {k: str(body[k]).strip() for k in ("client_id", "client_secret", "privacy") if body.get(k) is not None}
    if str(kw.get("client_secret", "")).startswith("••") or kw.get("client_secret") == "":
        kw.pop("client_secret", None)
    publish.update(platform, **kw)
    return publish.public()[platform]


@app.post("/api/publish/{platform}/connect")
def pub_connect(platform: str, request: Request):
    try:
        url = publish.auth_url(platform, str(request.base_url).rstrip("/") + "/oauth/callback")
    except publish.PublishError as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_cb(state: str = "", code: str = "", error: str = "", error_description: str = ""):
    def page(msg, ok):
        col = "#C2F54C" if ok else "#ff7a7a"
        return HTMLResponse(f"<html><body style='background:#141D25;color:#E8EEF3;font-family:system-ui;display:grid;place-items:center;"
                            f"height:100vh;margin:0'><div style='text-align:center'><h2 style='color:{col}'>{msg}</h2>"
                            "<p>You can close this tab and go back to Aru Clips.</p></div></body></html>")
    if error or not code:
        return page("Sign-in was cancelled: " + (error_description or error or "no code"), False)
    try:
        p = publish.finish_auth(state, code)
        return page(f"{publish.NAMES[p]} is connected", True)
    except Exception as e:
        return page(str(e)[:300], False)


@app.post("/api/publish/{platform}/token")
def pub_token(platform: str, body: dict = Body(...)):
    try:
        return {"user": publish.set_token(platform, body.get("token", ""))}
    except Exception as e:
        raise HTTPException(400, str(e)[:300])


@app.post("/api/publish/{platform}/disconnect")
def pub_disconnect(platform: str):
    publish.disconnect(platform)
    return {"ok": True}


@app.post("/api/publish/auto")
def pub_auto(body: dict = Body(...)):
    publish.set_auto(body.get("on"), body.get("min_score", 80), body.get("platforms") or [])
    return publish.public()["auto"]


@app.post("/api/jobs/{jid}/publish")
def pub_clips(jid: str, body: dict = Body(...)):
    j = job_or_404(jid)
    ns = body.get("ns")
    if ns is None:  # everything rendered, optionally above a score
        mn = int(body.get("min_score") or 0)
        ns = [c["n"] for c in j.data.get("clips", []) if c.get("file") and not c.get("pending") and int(c.get("score") or 0) >= mn]
    try:
        k = jobs.publish_clips(j, ns, body.get("platforms") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not k:
        raise HTTPException(400, "Nothing new to publish (clips must be rendered, and each goes to a platform once)")
    return {"queued": k}


@app.post("/api/reveal")
def reveal(body: dict = Body(...)):
    p = within(output_dir(), body.get("path") or "")
    if not p.exists():
        p = output_dir()
    if IS_WIN:
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ======================================================================= fonts
def font_family(p):
    try:
        from fontTools.ttLib import TTFont
        n = TTFont(str(p), lazy=True)["name"]
        fam = n.getDebugName(4) or n.getDebugName(1) or p.stem
        return re.sub(r"\s+Regular$", "", fam)
    except Exception:
        return p.stem


def font_em(p):
    """How big one em is for a given ASS font size. libass sizes text by the font's Windows line height, the browser by em."""
    try:
        from fontTools.ttLib import TTFont
        t = TTFont(str(p), lazy=True)
        upem, o, h = t["head"].unitsPerEm, t["OS/2"], t["hhea"]
        tall = (o.usWinAscent + o.usWinDescent) or (h.ascent - h.descent)
        return round(upem / tall, 4) if tall > 0 else 0.8
    except Exception:
        return 0.8


@app.get("/api/fonts")
def fonts():
    out = []
    for d, src in ((ASSETS / "fonts", "bundled"), (USER_FONTS, "mine")):
        for p in sorted(d.glob("*")):
            if p.suffix.lower() in (".ttf", ".otf"):
                out.append({"file": p.name, "family": font_family(p), "source": src, "em": font_em(p)})
    have = {pathlib.Path(x["file"]).name.lower() for x in out}
    cat = [{"id": i, "name": n, "tag": t, "installed": pathlib.Path(path).name.lower() in have} for i, n, t, path in CATALOG]
    return {"fonts": out, "catalog": cat}


@app.post("/api/fonts")
async def add_font(file: UploadFile = File(...)):
    name = safe_name(file.filename)
    if not name.lower().endswith((".ttf", ".otf")):
        raise HTTPException(400, "Use a .ttf or .otf font file")
    p = USER_FONTS / name
    p.write_bytes(await file.read())
    return {"ok": True, "family": font_family(p)}


@app.post("/api/fonts/get/{fid}")
def get_font(fid: str):
    item = next((c for c in CATALOG if c[0] == fid), None)
    if not item:
        raise HTTPException(404, "Unknown font")
    try:
        r = requests.get(GF + item[3], timeout=60)
        r.raise_for_status()
    except Exception:
        raise HTTPException(502, "Couldn't download the font. Check your internet connection.")
    (USER_FONTS / pathlib.Path(item[3]).name).write_bytes(r.content)
    return {"ok": True}


@app.delete("/api/fonts/{name}")
def del_font(name: str):
    p = USER_FONTS / safe_name(name)
    if p.exists():
        p.unlink()
    return {"ok": True}


# ======================================================================= sfx
@app.get("/api/sfx")
def sfx_list():
    out = []
    for p in sorted((ASSETS / "sfx").glob("*.wav"), key=lambda p: list(SFX_META).index(p.stem) if p.stem in SFX_META else 99):
        lab, cat = SFX_META.get(p.stem, (p.stem.title(), "UI"))
        out.append({"id": f"builtin/{p.name}", "label": lab, "category": cat, "source": "builtin"})
    for p in sorted(USER_SFX.glob("*")):
        if p.is_file():
            out.append({"id": f"mine/{p.name}", "label": p.stem.replace("_", " "), "category": "Mine", "source": "mine"})
    return out


@app.post("/api/sfx")
async def add_sfx(file: UploadFile = File(...)):
    name = safe_name(file.filename)
    if not name.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
        raise HTTPException(400, "Use an mp3, wav, m4a, aac, ogg or flac file")
    (USER_SFX / name).write_bytes(await file.read())
    return {"ok": True}


@app.delete("/api/sfx/{sid:path}")
def del_sfx(sid: str):
    if sid.startswith("mine/"):
        p = USER_SFX / safe_name(sid.split("/", 1)[1])
        if p.exists():
            p.unlink()
    return {"ok": True}


# ======================================================================= gameplay fillers
@app.get("/api/fillers")
def fillers():
    out = []
    for p in sorted(FILLERS.glob("*")):
        if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
            out.append({"name": p.name, "label": p.stem, "size_mb": round(p.stat().st_size / 1e6, 1)})
    return out


@app.post("/api/fillers")
async def add_filler(file: UploadFile = File(...)):
    name = safe_name(file.filename)
    if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v")):
        raise HTTPException(400, "Use an mp4 or mov video")
    p = FILLERS / name
    with open(p, "wb") as f:
        while chunk := await file.read(8 << 20):
            f.write(chunk)
    return {"ok": True, "name": name}


@app.delete("/api/fillers/{name}")
def del_filler(name: str):
    p = FILLERS / safe_name(name)
    if p.exists():
        p.unlink()
    (FILLERS / ".thumbs" / (p.name + ".jpg")).unlink(missing_ok=True)
    return {"ok": True}


def _already_running(port):
    """True when Aru Clips is already open on this port (double-clicked twice)."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _free_port(first):
    import socket
    for port in [first] + list(range(8766, 8800)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            try:
                sk.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def _log_to_file():
    """Copy everything printed to app.log, so a friend can send it when something goes wrong."""
    import logging
    log = DATA / "app.log"
    try:
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(DATA / "app.old.log")
        f = open(log, "a", encoding="utf-8", buffering=1)
    except Exception:
        return

    class Tee:
        def __init__(self, a):
            self.a = a
        def write(self, x):
            try:
                self.a and self.a.write(x)
            except Exception:
                pass
            try:
                f.write(x)
            except Exception:
                pass
        def flush(self):
            try:
                self.a and self.a.flush()
            except Exception:
                pass
        def isatty(self):
            try:
                return bool(self.a and self.a.isatty())
            except Exception:
                return False
        def __getattr__(self, k):
            if self.a is None:  # windowless start (pythonw): there is no console
                raise AttributeError(k)
            return getattr(self.a, k)

    sys.stdout, sys.stderr = Tee(sys.stdout), Tee(sys.stderr)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    print(f"\n---- Aru Clips start {time.strftime('%Y-%m-%d %H:%M:%S')} | Python {sys.version.split()[0]} | {sys.platform} | ffmpeg: {FFMPEG or 'NOT FOUND'}")


if __name__ == "__main__":
    import uvicorn
    _log_to_file()
    port = PORT
    if _already_running(port):
        print(f"\n  Aru Clips is already open at http://127.0.0.1:{port} . Opening it in your browser.\n")
        webbrowser.open(f"http://127.0.0.1:{port}")
        sys.exit(0)
    port = _free_port(port)
    if not port:
        print("  No free network port between 8765 and 8800. Close other apps and try again.")
        sys.exit(1)
    if not FFMPEG:
        print("  ffmpeg was not found. Run Install.bat again (it downloads ffmpeg into the tools folder).")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    watch.start(jobs.create, lambda jid: bool(jobs.get(jid)) and jobs.get(jid).get("status") in ("running", "queued", "paused"))
    print(f"\n  Aru Clips is running at http://127.0.0.1:{port}\n  Keep this window open while you use the app.\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
