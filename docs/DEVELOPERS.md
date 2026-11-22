# Aru Clips · for developers

Aru Clips is source-available, not open source: see [LICENSE](../LICENSE). This page is for the author and anyone
given permission to work on it.

## Layout

```
app/              the app (Python)
  server.py       FastAPI server and every /api route; serves the interface
  jobs.py         projects: downloading, transcribing, picking, rendering, live clipping, publishing, music check
  picker.py       finding and scoring moments (AI prompts, audience signals, keeping the cause before a reaction)
  render.py       one clip: reframing, captions and titles (libass), effects, sound, encoding
  media.py        yt-dlp downloads (sound, preview, sections), ffmpeg helpers, live chat
  ai.py           Groq, Gemini, OpenAI-style backups, Ollama, Whisper; vision and audio questions to Gemini
  taste.py        learning from star ratings and YouTube results
  analytics.py    the daily YouTube analytics check
  niche.py        studying the most-viewed Shorts in a niche
  vision.py       the picture check (frames of the best candidates)
  music.py        spotting songs and taking them out (MDX-Net vocal model on onnxruntime)
  publish.py      YouTube and Instagram uploads, scheduling slots
  signals.py      chat and replay signals, live stream finder
  watch.py        the streamer watchlist
  secure.py       saved keys: Windows DPAPI, macOS Keychain
  config.py       paths, settings, the ORIGIN credit
  static/         the whole interface: index.html
  assets/         fonts, sound effects, face-detection model, logo
launcher/         the Windows exe and the Mac app: a Go program that installs a private Python, the packages and
                  ffmpeg on first run, then starts the app (main.go, platform_windows.go, platform_darwin.go)
tests/run_tests.py   the test suite
download/         what users download: AruClips.exe (Windows), Aru-Clips-mac.zip (Mac)
docs/             the user guide (PDF) and these notes
```

## Run from source

Any OS: `pip install -r requirements.txt`, install ffmpeg, then `python app/server.py` (opens http://127.0.0.1:8765).

## Build

Needs Go 1.22+ and `rsrc` (`go install github.com/akavel/rsrc@latest`), from Linux or macOS.

1. `sh launcher/build.sh` packs `app/` and `requirements.txt` into the launcher, stamps `app/VERSION` and writes
   `dist/AruClips.exe`.
2. `MAKEFAT=/path/to/makefat sh launcher/build_mac.sh` builds the Mac launcher for Apple Silicon and Intel, joins them
   with [makefat](https://github.com/randall77/makefat) and writes `dist/Aru-Clips-mac.zip` with `Aru Clips.app`.
3. Copy both into `download/`.

The Mac app isn't signed or notarised (that needs an Apple Developer account), so users open it with right-click →
Open, or System Settings → Privacy & Security → Open Anyway. The guide explains it.

## Tests

Run `python tests/run_tests.py` before every push (needs ffmpeg and the packages in `requirements.txt`). It makes its
own test videos and fakes the AI, transcription, downloads and uploads, then checks: the local-server safety checks,
post text, a video project with pause, resume, a marked clip, ratings, spaced and scheduled publishing, start over and
delete, a long video clipped hour by hour, a live stream with the player feed, a marked clip, pause and resume, the
daily YouTube analytics check, niche research, the picture check, downloading only the needed parts (also a long
video clipped 3 hours of sound at a time), and the music check. Run one suite with `python tests/run_tests.py PartsOnly`.

## Still open

See [TODO.md](TODO.md).

## Credits

Some ideas in live clipping, chat filtering, loudness and caption masking were adapted from
[gtavi-claudecode](https://github.com/Bodila51/gtavi-claudecode) by Bodila51 (MIT license); those portions stay MIT.
