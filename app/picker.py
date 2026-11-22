"""Finds the most viral moments in a transcript (AI first, smart heuristics as backup)."""
import math, re
import numpy as np
import ai, signals, taste

PROFILES = {
    "gaming": """
This is GAMING content. Speech is only half the story: the best gaming clips are often a play, a fail or a reaction
where little is said. Use the chat and hype signals to find them and the transcript to frame them. Great gaming clips:
clutch plays and wins, embarrassing fails and deaths, rage and screaming, laughing fits, insane luck, bugs and glitches,
trolling and funny interactions, first reactions to something new. Start 1-2 seconds before the action: the first 2 seconds
decide whether a viewer stays or swipes away, so never open on calm set-up. End on the reaction or the punchline, not after
it fades.""",
    "gta6": """
The game is GTA 6 (Grand Theft Auto VI: Vice City, the state of Leonida, protagonists Lucia and Jason). GTA 6 clips that
perform: wild police chases and wanted-level chaos, heists and robberies going right or horribly wrong, crashes,
ragdoll and physics madness, hilarious NPC reactions and conversations, the in-game social media and radio parodies,
jaw-dropping realism (weather, water, crowds, faces, cars) and "GTA 5 could never" comparisons, hidden details and
easter eggs, first time seeing a new area or feature, money and car flexing, glitches, and the streamer's honest
first reactions to the game. Name the character in the title when one is involved (Lucia, Jason).""",
}


def profile_text(profile, game):
    t = ""
    if profile == "gaming":
        t += PROFILES["gaming"]
        if game == "gta6":
            t += PROFILES["gta6"]
    return t

# Moments with something at stake and the outcome still open: what worked on the creator's channel
# ("Lucia ALMOST dies", "GTA6 Standoff: 'Don't Run' Backfire", "When the odds are impossible").
STAKES_WORDS = re.compile(
    r"\b(almost|nearly|barely|don'?t (run|move|shoot|jump|do it|go)|watch out|look out|get down|get in|hold on|wait wait|"
    r"no no+|oh no|please|we'?re (dead|done|cooked|screwed)|i'?m (dead|cooked|done)|(he|she)'?s (gonna|about to)|"
    r"cops?|police|swat|wanted|(five|5) stars?|chase|chasing|crash(ed|ing)?|stand ?off|shoot(ing|out)?|gun|heist|robbery|"
    r"bank|escape|getaway|last (chance|life|shot)|impossible|no way out|trapped|surrounded|jump(ing)? off|bridge|"
    r"save (her|him|me)|lucia|jason)\b", re.I)


def stakes_moments(segs, min_hits=2, window=20.0):
    """Times where the talk shows something at stake (danger, a standoff, a chase, impossible odds)."""
    out = []
    for i, g in enumerate(segs):
        txt = " ".join(x["text"] for x in segs[i:i + 4] if x["s"] - g["s"] <= window)
        hits = len(STAKES_WORDS.findall(txt))
        if hits >= min_hits and (not out or g["s"] - out[-1][0] > window):
            out.append((g["s"], hits))
    return out


HYPE_WORDS = re.compile(r"\b(haha+|lol|lmao|omg|oh my god|no way|what\?*!|wow|bro|let'?s go+|insane|crazy|wtf|holy)\b|\[(laughter|laughs|applause)\]", re.I)


def fmt(t):
    t = max(0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _windows(segs, max_chars=6000):  # small enough for free-tier token-per-minute limits
    cur, n, out = [], 0, []
    for s in segs:
        line = f"[{s['s']:.1f}-{s['e']:.1f}] {s['text']}"
        if n + len(line) > max_chars and cur:
            out.append(cur); cur, n = cur[-2:], sum(len(x[1]) for x in cur[-2:])
        cur.append((s, line)); n += len(line) + 1
    if cur:
        out.append(cur)
    return out


def _system(mode, lang, profile="general", game="any"):
    if mode in ("stream", "live"):
        focus = ("You cut gaming and IRL live streams into viral TikTok, Reels and Shorts clips. Hunt for the moments a viewer "
                 "would replay or send to a friend: big laughs, rage, screams, shocking reactions, clutch wins, fails, roasts, "
                 "chaotic chat moments. A reaction needs its cause in the clip: start a beat before the thing that triggers it.")
    else:
        focus = ("You cut long videos into viral TikTok, Reels and Shorts clips, the way the best clip editors and tools do: "
                 "only the few moments that would stop someone mid-scroll and get shared.")
    return (focus + """

How to judge a moment, most important first:
1. The first line is the hook. The clip's first sentence must grab attention by itself: a bold claim, a surprising number, a
   sharp question, a strong emotion, a cliffhanger. Skip small talk before it.
   Viewers must see WHY someone reacts: a reaction clip starts on the thing that causes it (what happened in the game, what
   someone said, the choice or danger), a beat before the reaction, never in the middle of the reaction itself. A scream or a
   laugh without its cause makes no sense to someone scrolling. Give the second the cause happens as "trigger".
2. It stands alone. Hook, then build, then payoff (answer, punchline, twist, lesson). Someone who never saw the full video
   understands it. No "like I said earlier", no set-up left hanging.
3. It carries a payload: funny, shocking, inspiring, infuriating, a hot take, a vivid story, a myth busted, or advice people can use.
4. People would comment on it or send it on. Curiosity gaps that get closed travel furthest.
Skip: intros, "like and subscribe", sponsor reads, housekeeping, inside jokes that need the rest of the video, and flat stretches
in the middle of a thought. On streams also skip the junk: raids and shout-outs to raiders, ad breaks, "starting soon" / BRB /
ending screens, reading donations or sub alerts, queue and loading screens, and dead air where nothing happens.

What to hunt for first: moments with something at stake and the outcome still open when the clip starts: a character
in danger or about to die, a standoff, a chase, a risky jump or choice, impossible odds, a plan about to go wrong. These
held viewers best ("Lucia ALMOST dies", "GTA6 Standoff: 'Don't Run' Backfire", "When the odds are impossible"). Moments
that are just commentary, opinions about the game, or a generic reaction with nothing at stake ("Reality Check", "He's
flying the burn!") lost viewers in the first seconds: score them low on hook and share even when the talk is lively.

Score every clip with the same four-part rubric, judged the same way in every batch (each part 0-25):
- "hook": do the first 2 seconds grab someone who is scrolling? Clear stakes with an open outcome score highest.
- "emotion": how strong and genuine is the feeling it gives a viewer: laughing fits, shock, rage, fear, hype, heartbreak,
  awe, cringe, a voice cracking. Raw, real emotion scores high even with few words; calm explaining scores low.
- "payoff": does it land (punchline, twist, result, answer) and make sense on its own?
- "share": would people replay it, comment on it or send it to a friend?
Anchors for each part: 22-25 one of the best moments you would find in any video, 15-21 clearly good, 8-14 average, 0-7 weak.
Most moments are average: use the full range.
Titles: name a character or the stakes and leave the outcome open, so viewers stay to see what happens. Titles like these
worked: "Lucia ALMOST dies", "GTA6 Standoff: 'Don't Run' Backfire", "When the odds are impossible". Generic labels flopped:
"Reality Check", "He's flying the burn!". Never give away the ending in the title.
"caption" is a one-line hook for the description that teases the moment without spoiling it (one emoji at most).
Answer with JSON only. Write titles, reasons and captions in the same
language as the transcript""" + (" (Bangla)" if lang == "bn" else "") + "." + profile_text(profile, game) + taste.prompt_text())


def _ask(window, n, mn, mx, mode, lang, prompt, hype, log, profile="general", game="any"):
    first, last = window[0][0]["s"], window[-1][0]["e"]
    hints = [h for h in hype if first <= h <= last]
    text = "\n".join(l for _, l in window)
    user = (f"Transcript. Each line is [START-END] in seconds from the start of the video:\n{text}\n\n"
            f"Pick up to {n} clips, only ones that truly have a hook and a payoff (fewer strong clips beat many weak ones). "
            f"Each clip is {mn:.0f} to {mx:.0f} seconds; vary the lengths to fit each moment rather than making them all the same. "
            f"Use START and END times from the lines above so no sentence is cut, START on the line that sets up the moment (so viewers see what causes the reaction) and END right after the payoff. "
            f"Clips must not overlap and should be spread across the whole transcript, not bunched at the start.\n")
    if hints:
        user += "Hype moments (loud audio, chat spikes or laughter) at seconds: " + ", ".join(str(int(h)) for h in hints[:25]) + ". Weigh these strongly.\n"
    if prompt:
        user += f"Extra instructions from the creator: {prompt}\n"
    user += ('Return JSON: {"clips":[{"start":seconds,"end":seconds,"title":"scroll-stopping title, max 8 words",'
             '"reason":"one sentence naming the hook and why it stops the scroll","hook":0-25,"emotion":0-25,"payoff":0-25,"share":0-25,'
             '"caption":"one-line hook that teases without spoiling","trigger":seconds when the cause of the reaction happens}]}')
    data, eng = ai.chat_json([{"role": "system", "content": _system(mode, lang, profile, game)}, {"role": "user", "content": user}], log=log)
    out = []
    for c in data.get("clips", []) if isinstance(data, dict) else []:
        try:
            out.append(dict(_clip_from(c), picked_by=eng))
        except Exception:
            continue
    return out, eng


WEIGHTS = taste.DEFAULT_WEIGHTS  # adds up to 4: 25 on every part = 100. Learned from star ratings once there are enough.


def rubric_score(c):
    """The clip's score from its four rubric parts, emotion weighted highest. Falls back to a plain score."""
    parts = {}
    W = taste.weights()
    for k in WEIGHTS:
        try:
            parts[k] = max(0.0, min(25.0, float(c.get(k))))
        except (TypeError, ValueError):
            pass
    if len(parts) >= 3:
        for k in WEIGHTS:
            parts.setdefault(k, sum(parts.values()) / len(parts))
        return int(round(sum(parts[k] * W[k] for k in WEIGHTS))), {k: int(round(v)) for k, v in parts.items()}
    try:
        return int(float(c.get("score", 60))), None
    except (TypeError, ValueError):
        return 60, None


def _clip_from(c):
    sc, parts = rubric_score(c)
    out = {"start": _sec(c["start"]), "end": _sec(c["end"]), "title": str(c.get("title", "")).strip(),
           "reason": str(c.get("reason", "")).strip(), "score": sc, "caption": str(c.get("caption", "")).strip(),
           "hashtags": [h if str(h).startswith("#") else "#" + str(h) for h in (c.get("hashtags") or [])][:6]}
    if parts:
        out["breakdown"] = parts
    try:
        if c.get("trigger") not in (None, ""):
            out["trigger"] = _sec(c["trigger"])
    except (TypeError, ValueError):
        pass
    return out


def keep_context(c, segs, mx, total, cause=None, lead=2.5):
    """Makes sure the clip shows what the reaction is about: when the start comes after (or right on) the cause,
    it moves back to the sentence a beat before it. The clip may run a little over the max length for that."""
    t = cause if cause is not None else c.get("trigger")
    if t is None or not (c["start"] - 30 <= t <= c["end"]):
        return c
    want = max(0.0, t - lead)
    if c["start"] <= want:
        return c
    g = next((g for g in segs if g["s"] <= want < g["e"] + 0.3), None)  # start on a sentence, not mid-word
    s = max(0.0, g["s"] - 0.1) if g and want - g["s"] <= 3 else want
    e = c["end"]
    if e - s > mx * 1.2:  # too long now: trim the tail, the cause matters more than the last seconds
        e = s + mx * 1.2
    c["start"], c["end"] = round(s, 2), round(min(total, e), 2)
    c["duration"] = round(c["end"] - c["start"], 1)
    c["context"] = True
    return c


def _sec(v):
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip()
    parts = [float(p) for p in v.split(":")]
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def snap(c, words, segs, mn, mx, total):
    """Tighten a clip to sentence/word boundaries and the length range."""
    s, e = c["start"], c["end"]
    starts = [g["s"] for g in segs] or [s]
    s = min(starts, key=lambda x: abs(x - s) + (0.6 if x > s + 0.5 else 0))
    if e - s > mx:
        e = s + mx
    if e - s < mn:
        e = s + mn
    ends = [g["e"] for g in segs if s + mn * 0.8 <= g["e"] <= s + mx + 0.3]
    if ends:
        e = min(ends, key=lambda x: abs(x - e))
    if words:
        inside = [w for w in words if w["s"] >= s - 0.05]
        if inside:
            s = max(0, inside[0]["s"] - 0.12)
        last = [w for w in words if w["e"] <= e + 0.4]
        if last:
            e = last[-1]["e"] + 0.35
    e = min(e, total or e)
    c["start"], c["end"] = round(max(0, s), 2), round(e, 2)
    c["duration"] = round(c["end"] - c["start"], 1)
    return c


def heuristic(words, segs, total, n, mn, mx, loud, hype):
    """No AI available: score windows by speech energy, loudness and hype."""
    if not segs:
        step = max(mx, 20)
        return [{"start": i * step, "end": min(total, i * step + mx), "score": 50, "title": f"Moment {i + 1}",
                 "reason": "Picked by audio energy.", "caption": "", "hashtags": []} for i in range(min(n, int(total // step) or 1))]
    L = len(loud)
    hyset = np.zeros(max(1, int(total) + 2))
    for h in hype:
        if 0 <= int(h) < len(hyset):
            hyset[int(h)] = 1
    cands = []
    target = (mn + mx) / 2
    for i, sg in enumerate(segs):
        s = sg["s"]
        e = s
        j = i
        while j < len(segs) and segs[j]["e"] - s <= target:
            e = segs[j]["e"]; j += 1
        if e - s < mn:
            continue
        ws = [w for w in words if s <= w["s"] < e]
        text = " ".join(g["text"] for g in segs[i:j])
        rate = len(ws) / max(1, e - s)
        lo = loud[int(s):min(L, int(e) + 1)] if L else np.array([-30])
        energy = float(np.mean(lo)) if len(lo) else -30
        score = (rate * 8 + (energy + 40) * 0.8 + 12 * hyset[int(s):int(e) + 1].sum() + 6 * len(HYPE_WORDS.findall(text))
                 + 7 * len(STAKES_WORDS.findall(text)))
        score += 5 if "?" in segs[i]["text"] else 0
        cands.append((score, s, e, text))
    cands.sort(reverse=True)
    picked = []
    for sc, s, e, text in cands:
        if all(e <= p["start"] or s >= p["end"] for p in picked):
            title = " ".join(text.split()[:7]).strip(" ,.")
            picked.append({"start": s, "end": e, "score": 0, "raw": sc, "title": title, "reason": "Picked by speech pace, energy and reactions (no AI engine was available).",
                           "caption": title, "hashtags": ["#viral", "#fyp", "#shorts"]})
        if len(picked) >= n:
            break
    if picked:
        hi, lo_ = max(p["raw"] for p in picked), min(p["raw"] for p in picked)
        for p in picked:
            p["score"] = int(60 + 30 * ((p.pop("raw") - lo_) / (hi - lo_ + 1e-9)))
    return picked


def hype_moments(loud, chat=None, words=None, bucket=10):
    t = []
    if len(loud) > 30:
        z = (loud - np.median(loud)) / (np.std(loud) + 1e-6)
        peaks = [i for i in range(1, len(z) - 1) if z[i] > 2.0 and z[i] >= z[i - 1] and z[i] >= z[i + 1]]
        t += sorted(peaks, key=lambda i: -z[i])[:40]
    if chat:
        vals = np.array(list(chat.values()), float)
        if len(vals) > 5:
            thr = np.median(vals) + 2 * np.std(vals)
            t += [k * bucket for k, v in sorted(chat.items(), key=lambda kv: -kv[1]) if v >= thr][:40]
    for w in words or []:
        if HYPE_WORDS.search(w["w"]):
            t.append(w["s"])
    return sorted(set(round(x) for x in t))


def pad_clip(c, segs, total, pre, post):
    """Adds build-up before and aftermath after a moment, without starting or ending in the middle of a sentence."""
    if not (pre or post):
        return c
    s, e = max(0.0, c["start"] - pre), min(total, c["end"] + post)
    g = next((g for g in segs if g["s"] < s < g["e"]), None)  # new start lands mid-sentence: back up to its start
    if g and c["start"] - g["s"] <= pre + 3:
        s = max(0.0, g["s"] - 0.1)
    g = next((g for g in segs if g["s"] < e < g["e"]), None)  # new end lands mid-sentence: finish it
    if g and g["e"] - c["end"] <= post + 3:
        e = min(total, g["e"] + 0.3)
    c["start"], c["end"] = round(s, 2), round(e, 2)
    c["duration"] = round(c["end"] - c["start"], 1)
    return c


SENT_END = re.compile(r"[.?!।…]['\")\]]*$")


def sentence_ends(words, segs):
    """Times where a sentence finishes: a word ending in . ? ! or a pause after a word. Only when the transcript has
    no punctuation are segment ends used too, and never one that falls inside a word."""
    ws = sorted(words, key=lambda w: w["s"])
    punct = sum(1 for w in ws if SENT_END.search(str(w["w"]).strip()))
    ends = set()
    for a, b in zip(ws, ws[1:] + [None]):
        if SENT_END.search(str(a["w"]).strip()) or b is None or b["s"] - a["e"] > 0.7:
            ends.add(round(a["e"], 2))
    if punct < max(2, len(ws) / 60):  # no punctuation to go by
        for g in segs:
            if not any(w["s"] < g["e"] - 0.05 and w["e"] > g["e"] + 0.05 for w in ws):
                ends.add(round(g["e"], 2))
    if not ws:
        ends = {round(g["e"], 2) for g in segs}
    return sorted(ends)


def finish_sentence(c, words, segs, mn, mx, total, reach=None):
    """Makes sure a clip never stops in the middle of a sentence: carries on to the end of the sentence being said,
    or, when that is too far, ends after the previous sentence instead."""
    if not (words or segs):
        return c
    s, e = c["start"], c["end"]
    speaking = [w for w in words if w["s"] < e - 0.05 and w["e"] > e + 0.05]  # a word is cut in half
    ends = sentence_ends(words, segs)
    before = [t for t in ends if s < t <= e + 0.05]
    last_end = before[-1] if before else s
    talking_after = [w for w in words if last_end + 0.05 < w["s"] < e - 0.05]
    if not speaking and not talking_after:
        return c  # it already stops in the pause after a sentence
    reach = reach if reach is not None else max(5.0, min(12.0, mx * 0.25))
    def tail(t):  # a little air after the last word, but never into the next one
        nxt = [w["s"] for w in words if w["s"] > t + 0.01]
        return min(t + 0.35, (nxt[0] - 0.03) if nxt else t + 0.35, total or t + 0.35)

    after = [t for t in ends if e < t <= e + reach]
    if after and after[0] - s <= mx + reach:
        e = tail(after[0])
    elif before and last_end - s >= max(3.0, mn * 0.8):
        e = tail(last_end)
    else:
        return c
    c["end"] = round(e, 2)
    c["duration"] = round(c["end"] - c["start"], 1)
    return c


def pick(tr, total, n, mn, mx, mode="video", prompt="", loud=None, hype=(), log=print, progress=lambda f: None,
         profile="general", game="any", pre=0.0, post=0.0):
    words, segs, lang = tr["words"], tr["segments"], tr.get("language", "en")
    loud = loud if loud is not None else np.array([-30.0])
    wins = _windows(segs)
    cands, engine = [], None
    try:
        for k, w in enumerate(wins):
            share = (w[-1][0]["e"] - w[0][0]["s"]) / max(1, total)
            want = max(2, math.ceil(n * 1.6 * share) + 1)
            got, engine = _ask(w, min(want, n + 3), mn, mx, mode, lang, prompt, list(hype), log, profile, game)
            cands += got
            progress((k + 1) / len(wins))
    except ai.AIUnavailable:
        log("No AI engine is available, so clips are picked by speech pace, loudness and reactions.")
    except Exception as e:
        log(f"AI picking failed ({str(e)[:120]}), using the backup picker")
    if not cands:
        cands = [dict(c, picked_by="backup picker (no AI)") for c in heuristic(words, segs, total, n, mn, mx, loud, hype)]
        engine = "heuristic"
    cands = [keep_context(snap(c, words, segs, mn, mx, total), segs, mx, total) for c in cands if c["end"] > c["start"]]
    cands = skip_junk(cands, words, segs, loud, log)
    cands.sort(key=lambda c: -c["score"])
    out = []
    for c in cands:
        if c["duration"] < max(3, mn * 0.6):
            continue
        if all(c["end"] <= o["start"] + 0.5 or c["start"] >= o["end"] - 0.5 for o in out):
            out.append(c)
        if len(out) >= n:
            break
    return [finish_sentence(pad_clip(c, segs, total, pre, post), words, segs, mn, mx, total) for c in out], engine


JUNK_RE = re.compile(r"\b(raid(ing|ers?|ed)?|thanks? for the raid|welcome (in )?raiders|ad break|ads? (are )?(playing|running)|"
                     r"(we'?ll|i'?ll) be right back|\bbrb\b|starting soon|stream (is )?starting|this video is sponsored|"
                     r"sponsored by|use (my )?code|link in (the )?(bio|description)|thanks? for the (sub|follow|bits|donation|dono|prime)|"
                     r"gifted \d+ subs?|hype train|end(ing)? (the|this) stream|that'?s it for (today|the stream))\b", re.I)


def skip_junk(cands, words, segs, loud=None, log=print):
    """Drops clips that are mostly stream junk (raids, ads, BRB, sub alerts, sponsor reads) or dead air."""
    out, dropped = [], 0
    for c in cands:
        a, b = c["start"], c["end"]
        text = " ".join(g["text"] for g in segs if g["e"] > a and g["s"] < b)
        hits = len(JUNK_RE.findall(text))
        nw = sum(1 for w in words if a <= w["s"] < b)
        quiet = False
        if loud is not None and len(loud) > 1:
            seg = np.asarray(loud)[int(max(0, a)):int(max(a + 1, b))]
            quiet = seg.size > 0 and float(np.mean(seg)) < -42
        if hits >= 2 or (hits == 1 and nw < 40) or (nw <= 3 and quiet):
            dropped += 1
            continue
        out.append(c)
    if dropped:
        log(f"Skipped {dropped} moment{'s' if dropped != 1 else ''} that looked like stream junk (raids, ads, BRB, alerts) or dead air")
    return out if out else cands


def describe(text, lang="en", profile="general", game="any", log=print):
    """Title, caption, hashtags and rubric score for a moment the user marked by hand."""
    fallback = {"title": " ".join(text.split()[:7]) or "Your clip", "caption": "", "hashtags": [], "score": 0}
    if not text.strip():
        return fallback
    user = (f"The creator marked this moment by hand. Transcript:\n{text[:3000]}\n\nWrite its title and post text, and score it "
            'with the rubric. Return JSON: {"title":"scroll-stopping title, max 8 words","reason":"one sentence",'
            '"hook":0-25,"emotion":0-25,"payoff":0-25,"share":0-25,"caption":"one-line hook that teases without spoiling"}')
    try:
        data, eng = ai.chat_json([{"role": "system", "content": _system("video", lang, profile, game)}, {"role": "user", "content": user}], log=log)
        c = _clip_from(dict(data, start=0, end=1))
        c.pop("start"); c.pop("end")
        c["reason"] = "You marked this moment. " + c.get("reason", "")
        return c
    except Exception as e:
        log(f"Couldn't title your clip with AI ({str(e)[:100]})")
        return fallback


# ---------------------------------------------------------------- audience-first picking
def _judge(batch, mn, mx, mode, lang, prompt, log, profile, game):
    """batch: list of candidate dicts with id, lo, hi, lines, chat, why. Returns AI clips keyed by id."""
    blocks = []
    for c in batch:
        b = [f"### Moment {c['id']}: peak at {c['peak']:.0f}s ({c['why'] or 'audio/transcript'})",
             f"Window you may use: {c['lo']:.1f}s to {c['hi']:.1f}s"]
        b.append("Transcript:\n" + ("\n".join(c["lines"]) if c["lines"] else "(no speech here)"))
        if c["chat"]:
            b.append("What chat was typing right after: " + " | ".join(c["chat"]))
        blocks.append("\n".join(b))
    user = ("The audience already flagged these moments: chat exploded, viewers replayed them, or the audio spiked. "
            "For each moment, find the best clip inside its window: it must contain the peak AND what caused it, so a viewer "
            "sees what the streamer is reacting to. Start a few seconds before the cause (the event, line or danger), never in "
            f"the middle of the reaction, and end on the payoff. Each clip is {mn:.0f} to {mx:.0f} seconds; use the "
            "transcript times so no sentence is cut. If a moment is weak or makes no sense without context, set \"skip\": true.\n\n"
            + "\n\n".join(blocks) + "\n\n")
    if prompt:
        user += f"Extra instructions from the creator: {prompt}\n"
    user += ('Return JSON: {"clips":[{"id":1,"skip":false,"start":seconds,"end":seconds,"title":"scroll-stopping title, max 8 words",'
             '"reason":"one sentence naming the hook and why the audience reacted","hook":0-25,"emotion":0-25,"payoff":0-25,"share":0-25,'
             '"caption":"one-line hook that teases without spoiling","trigger":seconds when the cause of the reaction happens}]}')
    data, eng = ai.chat_json([{"role": "system", "content": _system(mode, lang, profile, game)}, {"role": "user", "content": user}], log=log)
    out = {}
    for c in data.get("clips", []) if isinstance(data, dict) else []:
        try:
            if c.get("skip"):
                out[int(c["id"])] = None; continue
            out[int(c["id"])] = dict(_clip_from(c), picked_by=eng)
        except Exception:
            continue
    return out, eng


def soft_snap(c, segs, mn, mx, total):
    """Like snap, but never drags a clip away from its peak when there's little speech (gaming)."""
    s, e = c["start"], c["end"]
    near = [g["s"] for g in segs if abs(g["s"] - s) <= 2.5]
    if near:
        s = min(near, key=lambda x: abs(x - s)) - 0.1
    near = [g["e"] for g in segs if abs(g["e"] - e) <= 2.5]
    if near:
        e = min(near, key=lambda x: abs(x - e)) + 0.3
    if e - s > mx:
        e = s + mx
    if e - s < mn:
        e = min(total, s + mn)
    c["start"], c["end"] = round(max(0, s), 2), round(min(total, e), 2)
    c["duration"] = round(c["end"] - c["start"], 1)
    return c


def pick_audience(tr, total, n, mn, mx, mode="video", prompt="", chat=None, heat=None, loud=None, log=print,
                  progress=lambda f: None, profile="general", game="any", lag=6.0, baseline=None, pre=0.0, post=0.0):
    """Finds clips where the audience reacted (chat, most replayed, loudness) and lets the AI frame and judge them.
    Falls back to reading the transcript alone when there are no audience signals."""
    words, segs, lang = tr["words"], tr["segments"], tr.get("language", "en")
    if not chat and not heat:
        hype = hype_moments(loud if loud is not None else np.array([-30.0]), None, words) if mode in ("stream", "live") else []
        hype = sorted(set(hype) | {int(t) for t, _ in stakes_moments(segs)})
        return pick(tr, total, n, mn, mx, mode, prompt, loud, hype, log, progress, profile, game, pre, post)
    score, parts = signals.timeline(total, chat, heat, loud, lag, profile, baseline)
    pk = signals.peaks(score, max(n * 2, n + 4), max(mn, 20))
    src = [x for x in ("chat" if chat else "", "most-replayed graph" if heat else "") if x]
    log(f"Found {len(pk)} audience peaks from {' and '.join(src)}")
    if not pk:
        return pick(tr, total, n, mn, mx, mode, prompt, loud, [], log, progress, profile, game, pre, post)
    smax = max(s for _, s in pk)
    extra = [(t, h) for t, h in stakes_moments(segs) if all(abs(t - p) > max(mn, 20) for p, _ in pk)]
    extra = sorted(extra, key=lambda x: -x[1])[:max(2, n // 2)]
    if extra:
        log(f"Added {len(extra)} moment{'s' if len(extra) != 1 else ''} with something at stake from the transcript")
        pk = list(pk) + [(t + 5.0, smax * min(0.75, 0.35 + 0.1 * h)) for t, h in extra]
    cands = []
    for k, (t, sc) in enumerate(pk):
        lo, hi = max(0.0, t - mx * 0.85), min(total, t + mx * 0.6)
        lines = [f"[{g['s']:.1f}-{g['e']:.1f}] {g['text']}" for g in segs if g["e"] > lo and g["s"] < hi]
        cands.append({"id": k + 1, "peak": float(t), "lo": lo, "hi": hi, "lines": lines, "sig": sc / smax,
                      "chat": signals.chat_sample(chat, t - 4, t + 4, lag) if chat else [], "why": signals.describe(parts, t)})
    picked, engine = [], None
    try:
        B = 3  # moments per request: keeps each request under the free-tier token limits
        for b in range(0, len(cands), B):
            res, engine = _judge(cands[b:b + B], mn, mx, mode, lang, prompt, log, profile, game)
            for c in cands[b:b + B]:
                r = res.get(c["id"], "missing")
                if r is None:
                    continue
                if r == "missing":
                    r = None
                if r and not (r["start"] - 2 <= c["peak"] <= r["end"] + 2):
                    r["start"], r["end"] = max(c["lo"], c["peak"] - mx * 0.5), min(c["hi"], c["peak"] + mx * 0.35)
                if r and r["start"] > c["peak"] - 4:  # the reaction peak needs its cause on screen first
                    r.setdefault("trigger", c["peak"] - 2)
                if r:
                    a = taste.blend()
                    r["ai_score"], r["signal"] = r["score"], round(float(c["sig"]), 3)
                    r["score"] = int(round((1 - a) * r["score"] + a * (55 + 45 * c["sig"])))
                    r["reason"] = (r["reason"] + (f" Audience: {c['why']}." if c["why"] else "")).strip()
                    picked.append(r)
            progress(min(1, (b + B) / len(cands)))
    except ai.AIUnavailable:
        log("No AI engine is available, so clips are cut around the audience peaks.")
    except Exception as e:
        log(f"AI judging failed ({str(e)[:120]}), cutting around the audience peaks")
    if not picked:
        for c in cands:
            pre = min(mx * 0.55, max(mn * 0.5, 12))
            s0 = max(0, c["peak"] - pre); e0 = min(total, s0 + max(mn, min(mx, pre + 10)))
            txt = " ".join(l.split("] ", 1)[1] for l in c["lines"][:2] if "] " in l)
            picked.append({"start": s0, "end": e0, "score": int(55 + 40 * c["sig"]), "title": " ".join(txt.split()[:7]) or "Chat went crazy here",
                           "reason": f"Audience: {c['why'] or 'reaction peak'}.", "caption": "", "hashtags": ["#gaming"] if profile == "gaming" else [],
                           "picked_by": "audience signals (no AI)"})
        engine = engine or "signals"
    picked = [keep_context(soft_snap(c, segs, mn, mx, total), segs, mx, total) for c in picked]
    picked = skip_junk(picked, words, segs, loud, log)
    picked.sort(key=lambda c: -c["score"])
    out = []
    for c in picked:
        if c["duration"] < max(3, mn * 0.6):
            continue
        if all(c["end"] <= o["start"] + 0.5 or c["start"] >= o["end"] - 0.5 for o in out):
            out.append(c)
        if len(out) >= n:
            break
    if len(out) < n and segs:  # top up from the transcript if the audience flagged too few moments
        extra, _ = pick(tr, total, n, mn, mx, mode, prompt, loud, [], log, lambda f: None, profile, game)
        for c in extra:
            if len(out) >= n:
                break
            if all(c["end"] <= o["start"] + 0.5 or c["start"] >= o["end"] - 0.5 for o in out):
                out.append(c)
    return [finish_sentence(pad_clip(c, segs, total, pre, post), words, segs, mn, mx, total) for c in out], engine


# ---------------------------------------------------------------- caption helpers
def translate_captions(items, target="bn", log=print):
    """items: list of caption strings. Returns list in target language or None."""
    lang = {"bn": "Bangla (Bengali script)", "en": "English"}[target]
    try:
        out_, _ = ai.chat_json([{"role": "system", "content": "You translate social media captions naturally, keeping emoji and hashtags. JSON only."},
                          {"role": "user", "content": f"Translate each caption to {lang}. Return {{\"items\":[...]}} in the same order.\n" +
                           "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))}], log=log)
        out = out_.get("items")
        if isinstance(out, list) and len(out) == len(items):
            return [str(x) for x in out]
    except Exception as e:
        log(f"Translation failed: {str(e)[:100]}")
    return None


_BN_V = {"অ": "o", "আ": "a", "ই": "i", "ঈ": "i", "উ": "u", "ঊ": "u", "ঋ": "ri", "এ": "e", "ঐ": "oi", "ও": "o", "ঔ": "ou"}
_BN_S = {"া": "a", "ি": "i", "ী": "i", "ু": "u", "ূ": "u", "ৃ": "ri", "ে": "e", "ৈ": "oi", "ো": "o", "ৌ": "ou"}
_BN_C = {"ক": "k", "খ": "kh", "গ": "g", "ঘ": "gh", "ঙ": "ng", "চ": "ch", "ছ": "chh", "জ": "j", "ঝ": "jh", "ঞ": "n", "ট": "t", "ঠ": "th",
         "ড": "d", "ঢ": "dh", "ণ": "n", "ত": "t", "থ": "th", "দ": "d", "ধ": "dh", "ন": "n", "প": "p", "ফ": "f", "ব": "b", "ভ": "bh",
         "ম": "m", "য": "j", "র": "r", "ল": "l", "শ": "sh", "ষ": "sh", "স": "s", "হ": "h", "ড়": "r", "ঢ়": "rh", "য়": "y", "ৎ": "t"}
_BN_X = {"ং": "ng", "ঃ": "h", "ঁ": "n", "্": "", "।": ".", "০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4", "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9"}


def banglish_word(w):
    w = w.replace("য়", "য়").replace("ড়", "ড়").replace("ঢ়", "ঢ়")
    out, i = [], 0
    while i < len(w):
        ch = w[i]
        if ch in _BN_C:
            out.append(_BN_C[ch])
            nxt = w[i + 1] if i + 1 < len(w) else ""
            if nxt in _BN_S:
                out.append(_BN_S[nxt]); i += 1
            elif nxt == "্" and i + 2 < len(w) and w[i + 2] == "য":
                out.append(_BN_C[ch]); i += 2  # য-phala doubles the consonant: সত্যি -> sotti
                if i + 1 < len(w) and w[i + 1] in _BN_S:
                    out.append(_BN_S[w[i + 1]]); i += 1
            elif nxt == "্":
                i += 1
            elif nxt and (nxt in _BN_C or nxt in _BN_X and nxt not in "্।") and i + 2 < len(w):
                out.append("o")
            elif not nxt and len(out) <= 1:
                out.append("o")
        elif ch in _BN_V:
            out.append(_BN_V[ch])
        elif ch in _BN_S:
            out.append(_BN_S[ch])
        else:
            out.append(_BN_X.get(ch, ch))
        i += 1
    return "".join(out)


def banglish(words, log=print):
    """Converts Bangla words to Latin letters, using AI when it is available."""
    uniq = sorted({w["w"] for w in words if re.search(r"[ঀ-৿]", w["w"])})
    table = {}
    if uniq:
        try:
            for k in range(0, len(uniq), 300):
                part = uniq[k:k + 300]
                mp, _ = ai.chat_json([{"role": "system", "content": "You write Bangla words in casual Banglish (English letters) the way people text. JSON only."},
                                  {"role": "user", "content": 'Return {"map":{"bangla word":"banglish"}} for:\n' + "\n".join(part)}], log=log)
                table.update(mp.get("map", {}))
        except Exception:
            pass
    return [{**w, "w": table.get(w["w"]) or banglish_word(w["w"])} for w in words]
