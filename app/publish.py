"""Publishing clips to YouTube Shorts, Instagram Reels, TikTok and X with each platform's official API.

Each platform needs the creator's own developer app (free) because the platforms don't allow a shared key in a desktop
app. YouTube and X sign in through the browser (OAuth with a local callback); TikTok can too; Instagram uses a long-lived
access token pasted from Meta's developer dashboard. Tokens stay on this PC in accounts.json.
"""
import base64, hashlib, json, os, pathlib, secrets, threading, time, urllib.parse
import requests
from config import DATA

ACCOUNTS = DATA / "accounts.json"
PLATFORMS = ["youtube", "instagram", "tiktok", "x"]
NAMES = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "tiktok": "TikTok", "x": "X"}
_lock = threading.RLock()
_pending = {}  # OAuth state -> (platform, verifier, redirect)
_queue = []
_qcv = threading.Condition()
_worker = None


class PublishError(RuntimeError):
    pass


# ------------------------------------------------------------------ storage
def load():
    try:
        return json.loads(ACCOUNTS.read_text("utf-8"))
    except Exception:
        return {}


def _save(d):
    tmp = ACCOUNTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), "utf-8")
    os.replace(tmp, ACCOUNTS)


def update(platform, **kw):
    with _lock:
        d = load()
        d.setdefault(platform, {}).update(kw)
        _save(d)
        return d[platform]


def acct(platform):
    return load().get(platform, {})


def public():
    """What Settings shows: never the secrets themselves."""
    d = load()
    out = {}
    for p in PLATFORMS:
        a = d.get(p, {})
        out[p] = {"name": NAMES[p], "connected": bool(a.get("access_token")), "user": a.get("user", ""),
                  "client_id": a.get("client_id", ""), "has_secret": bool(a.get("client_secret")),
                  "privacy": a.get("privacy", "public"), "error": a.get("error", "")}
    out["auto"] = d.get("_auto", {"on": False, "min_score": 80, "platforms": []})
    return out


def set_auto(on, min_score, platforms):
    with _lock:
        d = load()
        d["_auto"] = {"on": bool(on), "min_score": int(min_score), "platforms": [p for p in platforms if p in PLATFORMS]}
        _save(d)


def disconnect(platform):
    with _lock:
        d = load()
        a = d.get(platform, {})
        for k in ("access_token", "refresh_token", "expires", "user", "user_id", "error"):
            a.pop(k, None)
        _save(d)


# ------------------------------------------------------------------ sign in
def _pkce():
    v = secrets.token_urlsafe(64)[:96]
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def auth_url(platform, redirect):
    """Browser URL that starts the sign-in. redirect: http://127.0.0.1:PORT/oauth/callback (add it to the developer app)."""
    a = acct(platform)
    cid = a.get("client_id")
    if not cid:
        raise PublishError("Paste the app's client ID first")
    state = secrets.token_urlsafe(16)
    v, ch = _pkce()
    _pending[state] = (platform, v, redirect)
    if platform == "youtube":
        q = {"client_id": cid, "redirect_uri": redirect, "response_type": "code", "access_type": "offline", "prompt": "consent",
             "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
             "state": state, "code_challenge": ch, "code_challenge_method": "S256"}
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(q)
    if platform == "x":
        q = {"response_type": "code", "client_id": cid, "redirect_uri": redirect, "state": state,
             "scope": "tweet.read tweet.write users.read media.write offline.access", "code_challenge": ch, "code_challenge_method": "S256"}
        return "https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode(q)
    if platform == "tiktok":
        ch_hex = hashlib.sha256(v.encode()).hexdigest()  # TikTok's desktop flow wants the challenge as hex
        q = {"client_key": cid, "response_type": "code", "scope": "user.info.basic,video.publish,video.upload",
             "redirect_uri": redirect, "state": state, "code_challenge": ch_hex, "code_challenge_method": "S256"}
        return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(q)
    raise PublishError("Instagram connects with an access token: paste it in Settings")


def finish_auth(state, code):
    if state not in _pending:
        raise PublishError("This sign-in link expired. Press Connect again.")
    platform, v, redirect = _pending.pop(state)
    a = acct(platform)
    if platform == "youtube":
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": a["client_id"], "client_secret": a.get("client_secret", ""), "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": v}, timeout=30)
    elif platform == "x":
        auth = (a["client_id"], a["client_secret"]) if a.get("client_secret") else None
        r = requests.post("https://api.x.com/2/oauth2/token", auth=auth, data={
            "code": code, "grant_type": "authorization_code", "client_id": a["client_id"], "redirect_uri": redirect,
            "code_verifier": v}, timeout=30)
    elif platform == "tiktok":
        r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": a["client_id"], "client_secret": a.get("client_secret", ""), "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": v}, timeout=30)
    else:
        raise PublishError("unknown platform")
    t = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in t:
        raise PublishError(f"{NAMES[platform]} didn't sign in: " + str(t.get("error_description") or t.get("error") or r.text)[:300])
    _store_token(platform, t)
    who(platform)
    return platform


def _store_token(platform, t):
    kw = {"access_token": t["access_token"], "expires": time.time() + float(t.get("expires_in") or 3600) - 60, "error": ""}
    if t.get("refresh_token"):
        kw["refresh_token"] = t["refresh_token"]
    if t.get("open_id"):
        kw["user_id"] = t["open_id"]
    update(platform, **kw)


def set_token(platform, token):
    """Instagram (and any platform as a fallback): a long-lived access token pasted by the user."""
    token = "".join(str(token).split())
    update(platform, access_token=token, expires=time.time() + 55 * 86400, error="")
    try:
        return who(platform)
    except Exception:
        disconnect(platform)
        raise


def _token(platform):
    a = acct(platform)
    if not a.get("access_token"):
        raise PublishError(f"{NAMES[platform]} isn't connected. Connect it in Settings → Publishing.")
    if a.get("expires", 0) > time.time() or not a.get("refresh_token") and platform != "instagram":
        return a["access_token"]
    if platform == "youtube":
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": a["client_id"], "client_secret": a.get("client_secret", ""), "refresh_token": a["refresh_token"],
            "grant_type": "refresh_token"}, timeout=30)
    elif platform == "x":
        auth = (a["client_id"], a["client_secret"]) if a.get("client_secret") else None
        r = requests.post("https://api.x.com/2/oauth2/token", auth=auth, data={
            "refresh_token": a["refresh_token"], "grant_type": "refresh_token", "client_id": a["client_id"]}, timeout=30)
    elif platform == "tiktok":
        r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": a["client_id"], "client_secret": a.get("client_secret", ""), "refresh_token": a["refresh_token"],
            "grant_type": "refresh_token"}, timeout=30)
    else:  # Instagram long-lived tokens are refreshed for another 60 days
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": a["access_token"]}, timeout=30)
        if r.status_code >= 400:
            return a["access_token"]  # a Facebook-login token: keep using it until it stops working
    t = r.json() if r.content else {}
    if "access_token" not in t:
        update(platform, error="Sign-in expired, connect again")
        raise PublishError(f"{NAMES[platform]} sign-in expired. Connect it again in Settings → Publishing.")
    _store_token(platform, t)
    return t["access_token"]


def who(platform):
    """Fetches the account name (also checks the token works)."""
    tok = _token(platform)
    H = {"Authorization": "Bearer " + tok}
    if platform == "youtube":
        r = requests.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"}, headers=H, timeout=20)
        items = (r.json() if r.ok else {}).get("items") or []
        name = items[0]["snippet"]["title"] if items else ""
        if r.status_code >= 400:
            raise PublishError("YouTube: " + r.text[:200])
    elif platform == "x":
        r = requests.get("https://api.x.com/2/users/me", headers=H, timeout=20)
        if r.status_code >= 400:
            raise PublishError("X: " + r.text[:200])
        name = "@" + r.json()["data"]["username"]
    elif platform == "tiktok":
        r = requests.get("https://open.tiktokapis.com/v2/user/info/", params={"fields": "open_id,display_name"}, headers=H, timeout=20)
        d = (r.json() if r.content else {}).get("data", {}).get("user", {})
        if r.status_code >= 400:
            raise PublishError("TikTok: " + r.text[:200])
        name = d.get("display_name", "")
    else:
        name, uid = _ig_me(tok)
        update(platform, user_id=uid)
    update(platform, user=name)
    return name


def _ig_host(tok):
    return "https://graph.instagram.com" if tok.startswith("IG") else "https://graph.facebook.com"


def _ig_me(tok):
    host = _ig_host(tok)
    if "instagram" in host:
        r = requests.get(host + "/v21.0/me", params={"fields": "user_id,username", "access_token": tok}, timeout=20)
        if r.status_code >= 400:
            raise PublishError("Instagram didn't accept this token: " + r.text[:200])
        d = r.json()
        return "@" + d.get("username", ""), str(d.get("user_id") or d.get("id"))
    # Facebook-login token: find the Instagram professional account linked to one of the Pages
    r = requests.get(host + "/v21.0/me/accounts", params={"fields": "instagram_business_account{id,username}", "access_token": tok}, timeout=20)
    if r.status_code >= 400:
        raise PublishError("Instagram didn't accept this token: " + r.text[:200])
    for pg in r.json().get("data", []):
        ig = pg.get("instagram_business_account")
        if ig:
            return "@" + ig.get("username", ""), ig["id"]
    raise PublishError("No Instagram professional account is linked to this Facebook token")


# ------------------------------------------------------------------ uploads
class _Reader:
    """File reader that reports how much has been sent, so the clip card can show a percentage."""
    def __init__(self, f, size, prog, a=0.0, b=1.0):
        self.f, self.size, self.prog, self.done, self.a, self.b, self.last = f, max(1, size), prog, 0, a, b, -1

    def read(self, n=-1):
        d = self.f.read(n if n and n > 0 else 1 << 20)
        self.done += len(d)
        pct = int(100 * (self.a + (self.b - self.a) * self.done / self.size))
        if pct != self.last:
            self.last = pct
            self.prog(pct)
        return d

    def __len__(self):
        return self.size


def _noprog(pct, stage=None):
    pass


def _text(meta, limit):
    tags = " ".join(meta.get("hashtags") or [])
    body = (meta.get("caption") or meta.get("title") or "").strip()
    t = (body + ("\n\n" + tags if tags else "")).strip()
    return t[:limit]


def up_youtube(path, meta, log, prog=_noprog):
    tok = _token("youtube")
    a = acct("youtube")
    title = (meta.get("title") or "Clip")[:95]
    if "#shorts" not in title.lower() and len(title) < 88:
        title += " #Shorts"
    body = {"snippet": {"title": title, "description": _text(meta, 4900), "categoryId": "20" if meta.get("gaming") else "24",
                        "tags": [h.lstrip("#") for h in meta.get("hashtags") or []][:15]},
            "status": {"privacyStatus": {"private": "private", "unlisted": "unlisted"}.get(a.get("privacy"), "public"),
                       "selfDeclaredMadeForKids": False}}
    size = path.stat().st_size
    r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                      json=body, timeout=60, headers={"Authorization": "Bearer " + tok, "X-Upload-Content-Type": "video/mp4",
                                                      "X-Upload-Content-Length": str(size)})
    if r.status_code >= 400:
        raise PublishError("YouTube: " + r.text[:300])
    with open(path, "rb") as f:
        r2 = requests.put(r.headers["Location"], data=_Reader(f, size, prog), timeout=1800,
                          headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
    if r2.status_code >= 400:
        raise PublishError("YouTube upload: " + r2.text[:300])
    vid = r2.json()["id"]
    return f"https://youtube.com/shorts/{vid}"


def up_instagram(path, meta, log, prog=_noprog):
    tok = _token("instagram")
    a = acct("instagram")
    uid = a.get("user_id") or _ig_me(tok)[1]
    host = _ig_host(tok) + "/v21.0"
    r = requests.post(f"{host}/{uid}/media", data={"media_type": "REELS", "upload_type": "resumable", "caption": _text(meta, 2150),
                                                   "share_to_feed": "true", "access_token": tok}, timeout=60)
    d = r.json() if r.content else {}
    if r.status_code >= 400 or "id" not in d:
        raise PublishError("Instagram: " + str(d.get("error", {}).get("message") or r.text)[:300])
    cid = d["id"]
    uri = d.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v21.0/{cid}"
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = requests.post(uri, data=_Reader(f, size, prog), timeout=1800,
                          headers={"Authorization": "OAuth " + tok, "offset": "0", "file_size": str(size)})
    if r.status_code >= 400:
        raise PublishError("Instagram upload: " + r.text[:300])
    prog(100, "processing")
    for _ in range(120):  # Instagram processes the video before it can be published
        st = requests.get(f"{host}/{cid}", params={"fields": "status_code,status", "access_token": tok}, timeout=30).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError("Instagram couldn't process the video: " + str(st.get("status", ""))[:200])
        time.sleep(5)
    else:
        raise PublishError("Instagram took too long to process the video")
    r = requests.post(f"{host}/{uid}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60)
    d = r.json() if r.content else {}
    if r.status_code >= 400 or "id" not in d:
        raise PublishError("Instagram publish: " + str(d.get("error", {}).get("message") or r.text)[:300])
    try:
        link = requests.get(f"{host}/{d['id']}", params={"fields": "permalink", "access_token": tok}, timeout=20).json().get("permalink")
    except Exception:
        link = None
    return link or "https://instagram.com/" + a.get("user", "").lstrip("@")


def up_tiktok(path, meta, log, prog=_noprog):
    tok = _token("tiktok")
    a = acct("tiktok")
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8"}
    ci = requests.post("https://open.tiktokapis.com/v2/post/publish/creator_info/query/", headers=H, timeout=30).json().get("data", {})
    opts = ci.get("privacy_level_options") or ["SELF_ONLY"]
    want = {"private": "SELF_ONLY", "friends": "MUTUAL_FOLLOW_FRIENDS"}.get(a.get("privacy"), "PUBLIC_TO_EVERYONE")
    level = want if want in opts else ("SELF_ONLY" if "SELF_ONLY" in opts else opts[0])
    if level != want:
        log("TikTok only lets this app post privately until TikTok approves it (app audit), so the clip was posted as private")
    size = path.stat().st_size
    chunk = size if size <= 64 * 1024 * 1024 else 10 * 1024 * 1024
    n = max(1, size // chunk)
    body = {"post_info": {"title": _text(meta, 2200), "privacy_level": level, "disable_comment": False, "disable_duet": False,
                          "disable_stitch": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": n}}
    r = requests.post("https://open.tiktokapis.com/v2/post/publish/video/init/", headers=H, json=body, timeout=60)
    d = r.json() if r.content else {}
    if r.status_code >= 400 or not d.get("data", {}).get("upload_url"):
        raise PublishError("TikTok: " + str(d.get("error", {}).get("message") or r.text)[:300])
    url, pid = d["data"]["upload_url"], d["data"]["publish_id"]
    with open(path, "rb") as f:
        for i in range(n):
            a0 = i * chunk
            part = f.read(chunk if i < n - 1 else size - a0)
            r = requests.put(url, data=part, timeout=900, headers={"Content-Type": "video/mp4", "Content-Length": str(len(part)),
                                                                    "Content-Range": f"bytes {a0}-{a0 + len(part) - 1}/{size}"})
            if r.status_code >= 400:
                raise PublishError("TikTok upload: " + r.text[:300])
            prog(int(100 * (a0 + len(part)) / size))
    prog(100, "processing")
    for _ in range(90):
        st = requests.post("https://open.tiktokapis.com/v2/post/publish/status/fetch/", headers=H, json={"publish_id": pid}, timeout=30).json()
        s = st.get("data", {}).get("status")
        if s == "PUBLISH_COMPLETE":
            ids = st["data"].get("publicaly_available_post_id") or st["data"].get("publicly_available_post_id") or []
            return f"https://www.tiktok.com/@{a.get('user', '')}/video/{ids[0]}" if ids else "https://www.tiktok.com/"
        if s == "FAILED":
            raise PublishError("TikTok: " + str(st["data"].get("fail_reason", "failed")))
        time.sleep(5)
    return "https://www.tiktok.com/ (still processing)"


def up_x(path, meta, log, prog=_noprog):
    tok = _token("x")
    H = {"Authorization": "Bearer " + tok}
    size = path.stat().st_size
    r = requests.post("https://api.x.com/2/media/upload/initialize", headers=H, timeout=60,
                      json={"media_type": "video/mp4", "total_bytes": size, "media_category": "amplify_video"})
    d = r.json() if r.content else {}
    if r.status_code >= 400:
        raise PublishError("X: " + str(d.get("detail") or d.get("title") or r.text)[:300])
    mid = d["data"]["id"]
    chunk = 4 * 1024 * 1024
    with open(path, "rb") as f:
        i = 0
        while True:
            part = f.read(chunk)
            if not part:
                break
            r = requests.post(f"https://api.x.com/2/media/upload/{mid}/append", headers=H, timeout=600,
                              data={"segment_index": str(i)}, files={"media": ("clip.mp4", part, "application/octet-stream")})
            if r.status_code >= 400:
                raise PublishError("X upload: " + r.text[:300])
            i += 1
            prog(int(100 * min(size, i * chunk) / size))
    prog(100, "processing")
    r = requests.post(f"https://api.x.com/2/media/upload/{mid}/finalize", headers=H, timeout=60)
    info = (r.json() if r.content else {}).get("data", {}).get("processing_info")
    while info and info.get("state") in ("pending", "in_progress"):
        time.sleep(max(2, int(info.get("check_after_secs", 3))))
        st = requests.get("https://api.x.com/2/media/upload", headers=H, params={"command": "STATUS", "media_id": mid}, timeout=30).json()
        info = st.get("data", {}).get("processing_info")
    if info and info.get("state") == "failed":
        raise PublishError("X couldn't process the video: " + str(info.get("error", {}).get("message", ""))[:200])
    text = (meta.get("title") or "")
    tags = " ".join((meta.get("hashtags") or [])[:3])
    text = (text + (" " + tags if tags else ""))[:270]
    r = requests.post("https://api.x.com/2/tweets", headers=H, timeout=60, json={"text": text, "media": {"media_ids": [mid]}})
    d = r.json() if r.content else {}
    if r.status_code >= 400:
        raise PublishError("X post: " + str(d.get("detail") or d.get("title") or r.text)[:300])
    return f"https://x.com/{acct('x').get('user', '@i').lstrip('@')}/status/{d['data']['id']}"


UPLOADERS = {"youtube": up_youtube, "instagram": up_instagram, "tiktok": up_tiktok, "x": up_x}


# ------------------------------------------------------------------ queue (one upload at a time)
def enqueue(item):
    """item: {'path','meta','platforms','on_update': fn(platform, state_dict)}"""
    global _worker
    with _qcv:
        _queue.append(item)
        _qcv.notify()
    if not _worker or not _worker.is_alive():
        _worker = threading.Thread(target=_work, daemon=True)
        _worker.start()


def _work():
    while True:
        with _qcv:
            while not _queue:
                if not _qcv.wait(timeout=300):
                    return
            item = _queue.pop(0)
        log = item.get("log") or print
        for p in item["platforms"]:
            item["on_update"](p, {"state": "uploading", "pct": 0})
            try:
                def prog(pct, stage=None, p=p):
                    item["on_update"](p, {"state": "uploading", "pct": int(pct), "stage": stage or "uploading"})
                url = UPLOADERS[p](pathlib.Path(item["path"]), item["meta"], log, prog)
                item["on_update"](p, {"state": "done", "url": url, "at": int(time.time())})
                log(f"Published to {NAMES[p]}: {url}")
            except Exception as e:
                item["on_update"](p, {"state": "error", "error": str(e)[:300]})
                log(f"Publishing to {NAMES[p]} failed: {str(e)[:200]}")
