"""Background music: spotting it in a clip and removing it.

Songs playing on a stream get Shorts claimed or blocked. Two parts:
  detect()  - looks for music in a clip: the transcription's own music marks (♪, [Music]) plus how musical the sound is
              (a steady beat and held notes), so clips can be flagged before posting.
  voice_only() - keeps the voices and drops the music with a vocal-separation model (MDX-Net "Kim Vocal 2", run with
              onnxruntime on this PC). Game sounds without voices go too, so it is used where music was found or when
              the creator asks for it ("Mute background" on a clip).
"""
import pathlib, re, subprocess, sys, threading
import numpy as np
from config import DATA, FFMPEG, run

MODEL_URL = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/Kim_Vocal_2.onnx"
MODEL = DATA / "models" / "Kim_Vocal_2.onnx"
N_FFT, HOP, DIM_F, DIM_T, SR = 7680, 1024, 3072, 256, 44100
MARKS = re.compile(r"♪|♫|🎵|🎶|\[(music|singing|song)[^\]]*\]|\((music|singing|song)[^)]*\)", re.I)
_lock = threading.Lock()
_sess = {}


# ---------------------------------------------------------------- spotting music
def marked(words, a, b):
    """Seconds of the clip the transcription itself marked as music."""
    return sum(1 for w in words if a - 0.5 <= w["s"] <= b and MARKS.search(str(w.get("w", ""))))


ASK = """Listen to this short clip from a live stream (a streamer talking over gameplay).
Is a song or background music playing anywhere in it: a recognisable song, music with singing, a beat, or game radio
music? Speech, game sound effects, gunshots, cars and crowd noise are NOT music.
Return JSON: {"music": true/false, "song": true/false (a real song someone could own), "where": [[start_s, end_s]], "what": "a few words"}"""


def detect(src, a, b, words, log=print):
    """{'music': bool, 'sure': bool, 'what': str}. Uses the transcription's music marks, and Gemini listening to the
    clip when a Gemini key is set (it can tell a song from game sounds)."""
    import ai
    m = marked(words, a, b)
    out = {"music": m >= 2, "sure": m >= 2, "what": "marked as music in the transcript" if m >= 2 else "", "marks": m}
    try:
        r = run([FFMPEG, "-v", "error", "-ss", f"{a:.2f}", "-t", f"{min(b - a, 180):.2f}", "-i", str(src), "-vn", "-ac", "1",
                 "-ar", "16000", "-b:a", "48k", "-f", "mp3", "-"])
        if r.stdout:
            d, _ = ai.audio_json(ASK, r.stdout, "audio/mp3", log)
            if isinstance(d, dict) and "music" in d:
                heard = bool(d.get("music"))
                out.update(music=heard or out["music"], sure=heard or out["sure"], heard=heard, song=bool(d.get("song")),
                           what=str(d.get("what") or out["what"])[:80], where=d.get("where") or [])
    except ai.AIUnavailable:
        pass
    except Exception as e:
        log(f"Couldn't listen for music ({str(e)[:100]})")
    return out


# ---------------------------------------------------------------- keeping only the voices
def _ensure(log=print):
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        log("Installing the music remover (one time, about 15 MB)…")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "onnxruntime"], check=True)
    if not MODEL.exists() or MODEL.stat().st_size < 50_000_000:
        import requests
        log("Downloading the music remover's model (one time, about 64 MB)…")
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        tmp = MODEL.with_suffix(".part")
        with requests.get(MODEL_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(MODEL)


def _session():
    import onnxruntime as ort
    if "s" not in _sess:
        prov = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
        try:
            _sess["s"] = ort.InferenceSession(str(MODEL), providers=prov)
        except Exception:
            _sess["s"] = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    return _sess["s"]


_WIN = np.hanning(N_FFT + 1)[:-1].astype(np.float32)  # periodic Hann, like torch.hann_window


def _stft(x):
    """x: [2, n] -> [2, F, T] complex (centered, reflect padded)."""
    p = N_FFT // 2
    xp = np.pad(x, ((0, 0), (p, p)), mode="reflect")
    fr = np.lib.stride_tricks.sliding_window_view(xp, N_FFT, axis=1)[:, ::HOP]  # [2, T, N]
    return np.fft.rfft(fr * _WIN, axis=2).transpose(0, 2, 1)


def _istft(spec, length):
    fr = np.fft.irfft(spec.transpose(0, 2, 1), n=N_FFT, axis=2) * _WIN  # [2, T, N]
    T = fr.shape[1]
    out = np.zeros((2, N_FFT + HOP * (T - 1)), np.float32)
    norm = np.zeros(out.shape[1], np.float32)
    for t in range(T):
        out[:, t * HOP:t * HOP + N_FFT] += fr[:, t]
        norm[t * HOP:t * HOP + N_FFT] += _WIN ** 2
    out /= np.maximum(norm, 1e-8)
    p = N_FFT // 2
    return out[:, p:p + length]


def separate(mix):
    """mix: [2, n] float32 at 44.1 kHz -> the voices, same shape."""
    sess = _session()
    chunk = HOP * (DIM_T - 1)
    trim = N_FFT // 2
    gen = chunk - 2 * trim
    n = mix.shape[1]
    pad = gen - n % gen
    mp = np.concatenate([np.zeros((2, trim), np.float32), mix, np.zeros((2, pad + trim), np.float32)], 1)
    out = []
    for i in range(0, n + pad, gen):
        w = mp[:, i:i + chunk]
        sp = _stft(w)[:, :DIM_F]  # [2, F, T]
        x = np.stack([sp[0].real, sp[0].imag, sp[1].real, sp[1].imag])[None].astype(np.float32)
        y = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
        full = np.zeros((2, N_FFT // 2 + 1, DIM_T), np.complex64)
        full[0, :DIM_F] = y[0] + 1j * y[1]
        full[1, :DIM_F] = y[2] + 1j * y[3]
        out.append(_istft(full, chunk)[:, trim:-trim])
    return np.concatenate(out, 1)[:, :n]


def _merge(ranges, dur, pad=2.0):
    out = []
    for r in sorted((max(0.0, float(x[0]) - pad), min(dur, float(x[1]) + pad)) for x in ranges if len(x) >= 2 and float(x[1]) > float(x[0])):
        if out and r[0] <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], r[1]))
        else:
            out.append(r)
    return out


def voice_only(src, a, b, out_wav, log=print, ranges=None):
    """Writes a..b of src's sound with the music taken out (voices kept) to out_wav (44.1 kHz stereo).
    ranges (seconds from a): only those stretches are cleaned, the rest keeps its game sound; None = all of it."""
    with _lock:
        _ensure(log)
        r = run([FFMPEG, "-v", "error", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(src), "-vn", "-ac", "2", "-ar", str(SR),
                 "-af", "aresample=async=1000:first_pts=0", "-f", "f32le", "-"])
        mix = np.frombuffer(r.stdout or b"", np.float32).reshape(-1, 2).T.copy()
        if mix.shape[1] < SR:
            raise RuntimeError("no sound to clean")
        spans = _merge(ranges, mix.shape[1] / SR) if ranges else None
        if not spans:
            voc = separate(mix)
        else:  # clean only where the music is, with short crossfades into the original sound
            voc = mix.copy()
            fade = int(0.3 * SR)
            for s0, s1 in spans:
                i0, i1 = int(s0 * SR), int(s1 * SR)
                part = separate(np.ascontiguousarray(mix[:, i0:i1]))
                w = np.ones(i1 - i0, np.float32)
                k = min(fade, (i1 - i0) // 2)
                if k > 0 and i0 > 0:
                    w[:k] = np.linspace(0, 1, k)
                if k > 0 and i1 < mix.shape[1]:
                    w[-k:] = np.linspace(1, 0, k)
                voc[:, i0:i1] = part * w + mix[:, i0:i1] * (1 - w)
    p = subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-", str(out_wav)],
                       input=np.ascontiguousarray(voc.T).astype(np.float32).tobytes(), capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors="ignore")[-200:])
    return pathlib.Path(out_wav)
