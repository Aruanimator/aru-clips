# Aru Clips · Aru Animator

Turns long videos, past streams and live streams into vertical viral clips with captions, face tracking, layouts, effects and sound, all on your Windows PC.

## Download

**[Download AruClips.exe](https://github.com/Aruanimator/aru-clips/raw/main/download/AruClips.exe)** (Windows 10/11). Open it: the first run installs everything it needs and opens the app in your browser.

## Setup (once)

Works on any 64-bit Windows 10 or 11 PC. No admin rights, and it doesn't matter whether Python is already installed.

1. Unzip the folder (right-click, **Extract All**; don't run it from inside the zip). A short path like `C:\AruClips` is best.
2. Double-click **Install.bat**. It downloads its own private copy of Python, the app's packages and ffmpeg into this folder. Takes about 5 minutes. If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**.
3. Double-click **Start Aru Clips.bat**. The app opens in your browser. Keep the black window open while you use it.

If setup stops, run Install.bat again; it continues where it left off. Details are in `setup-log.txt`. If the app itself has a problem, send `app.log` from your `AruClips` user folder.

## Make it fast (free)

- Open **Settings**, paste a free Groq key from https://console.groq.com/keys and save. Groq transcribes and picks clips in seconds.
- Without a key, or when Groq's daily limit runs out, the app switches to your PC on its own:
  - **Whisper on this PC** is already installed. The first run downloads the model (Small is about 500 MB). Pick *Medium* in Settings for better Bangla.
  - **Ollama** (optional) gives offline AI clip picking: install it from https://ollama.com, then run `ollama pull qwen2.5:7b`. Without Ollama or Groq, clips are picked by speech pace, loudness and reactions.

## What it does

- **Sources:** YouTube, Twitch, Kick, TikTok, Instagram, Facebook, X and most other sites (via yt-dlp), or drop a file.
- **Only the parts it needs:** for YouTube and Twitch links it downloads the sound and a small 360p preview first, picks the moments, then downloads only those moments in full quality (with 20 seconds of room on each side for editing). A 6-hour stream takes about 2 GB instead of about 14 GB. You can still watch and mark moments yourself; a marked moment is downloaded in a few seconds. Turn it off in Settings → Downloads.
- **Past stream:** hunts for laughs, shouts and hype using audio loudness, reaction words and, on YouTube, the chat replay.
- **Live now:** records the stream, finds clips every few minutes, and lets you approve each one before it renders.
- **Layouts:** face tracking, center crop, blur fit, 2-frame split, podcast split (two speakers), gameplay split, face bubble, multicam cuts.
- **Captions:** Bold, Clean, Punchy, word-by-word highlight, any font (add your own .ttf/.otf, or install free fonts in one click). Bangla captions switch to a Bangla font by themselves, with an optional Banglish mode.
- **Effects:** slow zoom, punch-in, progress bar, flash, bouncy captions, film grain, vignette. **SFX** on the hook title, numbers or every caption.
- **Formats:** 9:16, 1:1, 4:5, 3:4, 16:9.
- **Watermark:** set on the New project page: the Aru Animator logo, your own image and/or text, or none, with position, size and opacity.
- **Pause, resume, start over, delete:** pause a project, close the app, and resume later from where it stopped (downloads, transcripts and picks are kept). Live projects reconnect and carry on.
- **Watch in the app and clip by hand:** the source video or the live stream plays in the project; drag on the bar under it (or press I and O) and press Clip it, or clip the last 30/60/90 seconds of a live stream. Auto clipping keeps running.
- **Clip board:** every project's clips on one page, waiting ones first, with one-click render or skip.
- **Streamers:** add the streamers you clip; the app checks every 3 minutes and starts a live project when one goes live.
- **Ratings that teach it:** rate clips with stars. After 6 ratings, scoring learns which rubric parts and how much the audience signal matter to you, and the AI sees examples of clips you loved and disliked.
- **Studies the niche:** while a video downloads (or a stream records) it finds the most-viewed Shorts about the same streamer and game, reads how the top ones open, and the AI works out which moments, titles and hooks win there. That brief guides picking and titling, shows on the project page, and is kept for 7 days per niche ("Study it again" refreshes it). Your own title rule (name the stakes, leave the outcome open) always wins over the trends.
- **Learns from YouTube analytics:** once a day (you pick the time) it reads views and % watched of the clips it posted. Clips 2 to 14 days old are judged against your own channel's spread and listed as "What worked" and "What flopped". Once 30 are judged, they also teach scoring which moments, titles and hooks work (before that, views are mostly luck). Needs the YouTube Analytics API turned on in your Google Cloud project.
- **Looks at the picture:** before the final pick, the best candidates are judged on 3 frames each by Gemini (or a Groq vision model): real action on screen moves a clip up, talk over a still screen moves it down. Without such an AI the pick is unchanged.
- **Publishing:** connect YouTube and Instagram with your own free developer apps, then publish one clip, the selected ones, everything above a score, or auto-publish new clips above a score.
- **Junk skipping:** raids, ad breaks, BRB/starting-soon screens, sub alerts, sponsor reads and dead air are left out.
- **Facebook / Instagram pack:** copies the clips with English and Bangla captions and an .srt subtitle file for each.

Clips are saved to `Videos\AruClips` (change it in Settings). Each project folder keeps the source, so **Edit** opens the clip editor: move or resize each screen, change the layout, format, timing, caption text, fonts, colours, effects and text placement, then re-render.

## Your logo

Replace `app\assets\brand\logo.png` with the real Aru Animator logo (a square PNG with a transparent background works best). It's used in the app and on every clip.

## Uninstall

Settings → **Uninstall Aru Clips…**, the tray icon → **Uninstall…**, or Windows Settings → Apps → Aru Clips. It removes the app and everything it installed (its own Python, the AI packages, ffmpeg, pip's download cache, shortcuts) and asks separately before deleting your settings/models and your clips.

## Private or age-restricted links

Public videos need no sign-in, so leave **Browser login** on None. For private or age-restricted videos, pick the browser you're signed in to; Firefox works best, because Chrome and Edge on Windows often block other apps from reading their sign-in. If the sign-in can't be read, the app tries again without it.

## For developers

```
app/            the app: FastAPI server (server.py), pipeline (jobs.py), AI (ai.py), clip picking (picker.py),
                rendering (render.py), downloads and ffmpeg helpers (media.py), chat and replay signals (signals.py)
app/static/     the whole interface, one index.html
app/assets/     fonts, sound effects, face-detection model, logo
launcher/       AruClips.exe: a small Go program that installs Python, the packages and ffmpeg on first run,
                then starts the app with a tray icon (main.go, platform_windows.go, setup.html)
Install.bat, Start Aru Clips.bat   the older two-step way to install and run without the exe
requirements.txt                    Python packages
```

Run from source on any OS: `pip install -r requirements.txt`, install ffmpeg, then `python app/server.py`.

Build the Windows exe (from Linux or macOS with Go 1.22+): `go install github.com/akavel/rsrc@latest`, then
`sh launcher/build.sh`. It packs `app/` and `requirements.txt` into the exe and writes `dist/AruClips.exe`.

### Tests

Run `python tests/run_tests.py` before every push (needs ffmpeg and the packages in `requirements.txt`). It builds its own test videos and fakes the AI, transcription and uploads, then checks: the local-server safety checks, post text, a video project with pause, resume, a marked clip, ratings, spaced and scheduled publishing, start over and delete, a long video clipped hour by hour, a live stream with the player feed, a marked clip, pause and resume, the daily YouTube analytics check, niche research and the picture check.

## License

Aru Clips is © 2026 Asiful Islam ([aru_animator](https://www.instagram.com/aru_animator/)). It is source-available, not open source: you can use the app and read the code, but copying or adapting the code needs permission. See [LICENSE](LICENSE).

Like the app? [Follow on Instagram](https://www.instagram.com/aru_animator/) or [connect on LinkedIn](https://www.linkedin.com/in/aruanimator/).

## Credits

Some ideas in live clipping, chat filtering, loudness and caption masking were adapted from
[gtavi-claudecode](https://github.com/Bodila51/gtavi-claudecode) by Bodila51 (MIT license).

## Notes

- Use gameplay or footage you own or have the rights to.
- If YouTube downloads stop working, close the app and start it again: Start Aru Clips.bat updates yt-dlp each time.
