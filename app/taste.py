"""Learning from the creator's star ratings.

Every rating is kept with the clip's rubric parts (hook, emotion, payoff, share), its audience signal and a short
transcript excerpt. From those the app learns:
  1. how much each rubric part matters to this creator (a small regression, pulled toward the defaults until there
     are enough ratings to trust it),
  2. how much the audience signal (chat spikes, most replayed, loudness) should count against the AI's opinion,
  3. a few loved and disliked examples that are shown to the AI, so it scores new moments the way this creator would.
"""
import json, threading, time
import numpy as np
from config import output_dir, load_settings

DEFAULT_WEIGHTS = {"hook": 1.0, "emotion": 1.3, "payoff": 0.9, "share": 0.8}  # adds up to 4: 25 on every part = 100
DEFAULT_BLEND = 0.35   # share of the final score that comes from audience signals
PRIOR = 10             # the defaults count as this many ratings
MIN_RATINGS = 6        # nothing changes before this many ratings

_lock = threading.Lock()
_cache = {"t": 0, "model": None}


def _file():
    return output_dir() / "ratings.json"


def load():
    try:
        return json.loads(_file().read_text("utf-8"))
    except Exception:
        return {}


def _save(d):
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    tmp.replace(_file())


def record(key, stars, clip, excerpt="", content="general", perf=None):
    """key: 'jobid:n' for the creator's stars, 'jobid:n:yt' for how it did on YouTube. stars 1-5, or 0 to remove."""
    with _lock:
        d = load()
        if not stars:
            d.pop(key, None)
        else:
            d[key] = {"stars": int(stars), "t": time.time(), "title": clip.get("title", ""), "content": content,
                      "breakdown": clip.get("breakdown"), "ai_score": clip.get("ai_score"), "signal": clip.get("signal"),
                      "score": clip.get("score"), "excerpt": " ".join(str(excerpt).split())[:220],
                      "manual": bool(clip.get("manual")), "hook": clip.get("caption", "")}
            if perf:
                d[key]["perf"] = perf
        _save(d)
        _cache["model"] = None


def reset():
    with _lock:
        _save({})
        _cache["model"] = None


def enabled():
    return bool(load_settings().get("learn_taste", True))


def model():
    """{'weights':..., 'blend':..., 'n':..., 'liked':[...], 'disliked':[...]} learned from the ratings."""
    if _cache["model"] and time.time() - _cache["t"] < 30:
        return _cache["model"]
    d = load()
    rows = list(d.values())
    out = {"weights": dict(DEFAULT_WEIGHTS), "blend": DEFAULT_BLEND, "n": len(rows), "learning": False, "liked": [], "disliked": [],
           "yt_good": [], "yt_bad": []}
    if enabled() and len(rows) >= MIN_RATINGS:
        out["learning"] = True
        ks = list(DEFAULT_WEIGHTS)
        fit = [r for r in rows if isinstance(r.get("breakdown"), dict) and all(k in r["breakdown"] for k in ks)]
        if len(fit) >= MIN_RATINGS:
            X = np.array([[float(r["breakdown"][k]) for k in ks] for r in fit])
            y = np.array([(r["stars"] - 1) / 4 * 100 for r in fit])
            Xc, yc = X - X.mean(0), y - y.mean()
            w0 = np.array([DEFAULT_WEIGHTS[k] for k in ks])
            lam = PRIOR * max(1e-6, float((Xc ** 2).sum()) / len(fit) / len(ks))
            w = np.linalg.solve(Xc.T @ Xc + lam * np.eye(len(ks)), Xc.T @ yc + lam * w0)
            w = np.clip(w, 0.25, None)
            w = w / w.sum() * 4  # keep the 0-100 scale, so the auto-approve bar still means the same thing
            out["weights"] = {k: round(float(v), 3) for k, v in zip(ks, w)}
        sig = [r for r in rows if r.get("signal") is not None and r.get("ai_score") is not None]
        if len(sig) >= MIN_RATINGS:
            stars = np.array([r["stars"] for r in sig], float)
            best, best_c = DEFAULT_BLEND, -2.0
            for a in np.arange(0.1, 0.65, 0.05):
                sc = np.array([(1 - a) * r["ai_score"] + a * (55 + 45 * r["signal"]) for r in sig])
                if sc.std() > 0 and stars.std() > 0:
                    c = float(np.corrcoef(sc, stars)[0, 1])
                    if c > best_c:
                        best, best_c = float(a), c
            out["blend"] = round((len(sig) * best + PRIOR * DEFAULT_BLEND) / (len(sig) + PRIOR), 3)
    recent = sorted(rows, key=lambda r: -r.get("t", 0))
    if enabled():
        mine = [r for r in recent if not r.get("perf")]
        out["liked"] = [r for r in mine if r["stars"] >= 4 and r.get("excerpt")][:3]
        out["disliked"] = [r for r in mine if r["stars"] <= 2 and r.get("excerpt")][:3]
        yt = [r for r in rows if r.get("perf")]  # how published clips actually did on YouTube
        best = sorted((r for r in yt if r["stars"] >= 4), key=lambda r: (-r["stars"], -(r["perf"].get("views") or 0)))
        worst = sorted((r for r in yt if r["stars"] <= 2), key=lambda r: (r["stars"], r["perf"].get("views") or 0))
        out["yt_good"], out["yt_bad"] = best[:4], worst[:3]
    _cache.update(t=time.time(), model=out)
    return out


def weights():
    return model()["weights"]


def blend():
    return model()["blend"]


def prompt_text():
    """Few-shot taste calibration added to the AI's instructions."""
    m = model()
    t = ""
    fmt = lambda r: (f'\n- "{r["title"]}"' + (f' (hook: "{r["hook"]}")' if r.get("hook") else "")
                     + f' {r["perf"].get("views") or 0:,} views, {r["perf"].get("avg_pct") or 0:.0f}% watched: {r.get("excerpt", "")[:140]}')
    if m.get("yt_good") or m.get("yt_bad"):
        t += "\n\nHow clips this app posted actually did on YouTube. Find moments, titles and hooks like the winners:"
        if m.get("yt_good"):
            t += "\nDid well:" + "".join(fmt(r) for r in m["yt_good"])
        if m.get("yt_bad"):
            t += "\nFlopped (avoid moments, titles and hooks like these):" + "".join(fmt(r) for r in m["yt_bad"])
    if not (m["liked"] or m["disliked"]):
        return t
    t += "\n\nThis creator rated earlier clips. Score new moments the way they would:"
    if m["liked"]:
        t += "\nLoved (5 stars style):" + "".join(f'\n- "{r["title"]}": {r["excerpt"]}' for r in m["liked"])
    if m["disliked"]:
        t += "\nDisliked (low stars):" + "".join(f'\n- "{r["title"]}": {r["excerpt"]}' for r in m["disliked"])
    return t


def summary():
    m = model()
    return {k: m[k] for k in ("weights", "blend", "n", "learning")} | {"min": MIN_RATINGS, "enabled": enabled(),
                                                                       "from_youtube": sum(1 for r in load().values() if r.get("perf")),
                                                                       "defaults": DEFAULT_WEIGHTS, "default_blend": DEFAULT_BLEND}
