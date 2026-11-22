"""Aru Clips test suite. Run before every push:  python tests/run_tests.py

Needs ffmpeg and the app's Python packages. Makes its own test videos and fakes the AI, transcription and uploads, so
it runs offline in a few minutes. Each test prints PASS/FAIL; the exit code is the number of failures.
"""
import os, sys, json, time, shutil, tempfile, threading, subprocess, unittest, pathlib, http.server, functools

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = pathlib.Path(tempfile.mkdtemp(prefix="aru_tests_"))
os.environ["ARU_DATA"] = str(TMP / "data")
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(ROOT / "app"))

import config  # noqa: E402
config.save_settings({"output_dir": str(TMP / "out"), "engine": "local"})
import ai, media, picker, jobs, publish, taste, post, secure, server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FF = config.FFMPEG or "ffmpeg"
MEDIA = TMP / "media"; MEDIA.mkdir()


def make_video(path, seconds, faststart=True):
    cmd = [FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
           "-c:a", "aac", "-shortest"] + (["-movflags", "+faststart"] if faststart else []) + [str(path)]
    subprocess.run(cmd, check=True)
    return path


VIDEO = make_video(MEDIA / "talk.mp4", 120)
LONG = make_video(MEDIA / "long.mp4", 170)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory, port):
    h = functools.partial(_Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


HTTP = serve(MEDIA, 8871)

# ---- fakes: no network AI, no real uploads
WORDS = [{"w": w, "s": i * 0.5, "e": i * 0.5 + 0.4} for i, w in enumerate(("Lucia is about to jump the bridge oh no " * 30).split())]
FAKE_TR = {"words": WORDS, "segments": [{"s": 0.0, "e": 60.0, "text": "Lucia is about to jump the bridge."}], "language": "en", "engine": "fake"}
ai.transcribe = lambda wav, total, lang="auto", log=print, progress=None: (progress and progress(0.5), FAKE_TR)[1]


def fake_pick(tr, total, n, *a, **k):
    return [{"start": 4.0, "end": 24.0, "duration": 20.0, "title": "Lucia ALMOST dies", "score": 88, "reason": "test",
             "caption": "She did NOT see this coming", "hashtags": [], "breakdown": {"hook": 22, "emotion": 23, "payoff": 20, "share": 20}},
            {"start": 40.0, "end": 58.0, "duration": 18.0, "title": "When the odds are impossible", "score": 72, "reason": "test",
             "caption": "Watch till the end", "hashtags": [], "breakdown": {"hook": 18, "emotion": 17, "payoff": 18, "share": 19}}][:max(1, n)], "fake"


picker.pick_audience = fake_pick
picker.describe = lambda *a, **k: {"title": "Your moment", "score": 70, "caption": "hook"}
POSTED = []


def fake_uploader(p):
    def up(path, meta, log, prog=lambda *a: None):
        prog(50); POSTED.append((p, meta)); return f"https://{p}.example/shorts/abc{len(POSTED)}"
    return up


publish.UPLOADERS.update({p: fake_uploader(p) for p in publish.PLATFORMS})


def wait(fn, timeout=240, step=0.5):
    t = time.time() + timeout
    while time.time() < t:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def fresh_out(name):
    config.save_settings({"output_dir": str(TMP / name)})
    jobs.JOBS.clear()


class Security(unittest.TestCase):
    def test_token_host_origin(self):
        c = TestClient(server.app, base_url="http://127.0.0.1:8765")
        self.assertEqual(c.get("/api/ping").status_code, 200)
        self.assertEqual(c.get("/api/jobs").status_code, 403)
        self.assertEqual(c.get("/api/jobs", headers={"X-Aru-Token": server.TOKEN}).status_code, 200)
        self.assertEqual(c.get("/api/jobs", headers={"X-Aru-Token": server.TOKEN, "Origin": "https://evil.example"}).status_code, 403)
        evil = TestClient(server.app, base_url="http://evil.example")
        self.assertEqual(evil.get("/api/ping").status_code, 403)
        self.assertIn(server.TOKEN, c.get("/").text)

    def test_secrets_stay_out_of_diagnostics(self):
        config.save_settings({"groq_api_key": "gsk_SECRETSECRETSECRET123"})
        c = TestClient(server.app, base_url="http://127.0.0.1:8765")
        txt = c.get("/api/diagnostics", headers={"X-Aru-Token": server.TOKEN}).json()["text"]
        self.assertNotIn("SECRETSECRET", txt)
        config.save_settings({"groq_api_key": ""})

    def test_secure_roundtrip(self):
        self.assertEqual(secure.unprotect(secure.protect("abc123")), "abc123")


class PostText(unittest.TestCase):
    def test_template(self):
        class J(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)
        j = J(opts={"game": "gta6"}, streamer="Kai Cenat", lang="en")
        p = post.build(j, {"title": "Lucia ALMOST dies", "caption": "She did NOT see this coming"}, "lucia jumps")
        self.assertIn("All credit to Kai Cenat", p["description"])
        tags = [t.lower() for t in p["description"].split() if t.startswith("#")]
        self.assertEqual(len(tags), len(set(tags)), "each hashtag once")
        self.assertEqual(p["hashtags"], ["#GTA6", "#KaiCenat", "#Shorts"])
        self.assertIn("Lucia GTA 6", p["tags"]); self.assertIn("Kai Cenat GTA 6", p["tags"])


class VideoProject(unittest.TestCase):
    def test_pause_resume_manual_rate_publish(self):
        fresh_out("out_vod")
        up = config.UPLOADS / "talk.mp4"; config.UPLOADS.mkdir(parents=True, exist_ok=True); shutil.copy(VIDEO, up)
        jid = jobs.create({"file": "talk.mp4", "title": "Test", "source_type": "stream", "num_clips": 2, "min_len": 15, "max_len": 30,
                           "crop_mode": "center", "format": "9:16", "game": "gta6", "content": "gaming"})
        j = jobs.JOBS[jid]
        self.assertTrue(wait(lambda: any(c.get("file") for c in j.data["clips"]) or j.get("status") in ("done", "error")))
        if j.get("status") == "running":
            jobs.pause(j)
            self.assertTrue(wait(lambda: j.get("status") == "paused" or j.get("status") == "done"))
            if j.get("status") == "paused":
                jobs.JOBS.clear(); jobs.load_all(); j = jobs.get(jid)
                self.assertTrue(jobs.resume(j))
        self.assertTrue(wait(lambda: j.get("status") in ("done", "error")))
        self.assertEqual(j.get("status"), "done", j.get("error"))
        files = [c for c in j.data["clips"] if c.get("file")]
        self.assertEqual(len(files), 2)
        self.assertEqual(len({c["n"] for c in j.data["clips"]}), len(j.data["clips"]), "no duplicate clips after resume")
        n = jobs.manual_clip(j, {"start": 70, "end": 85})
        self.assertTrue(wait(lambda: (j.clip(n) or {}).get("file")))
        jobs.rate(j, files[0]["n"], 5)
        self.assertGreaterEqual(taste.summary()["n"], 1)
        # publishing: spacing schedules the second clip; the scheduler sends it when due
        publish.update("youtube", access_token="t", expires=time.time() + 9999)
        publish.set_auto(False, 80, [], 30)
        d = publish.load(); d.pop("_slots", None); publish._save(d)
        POSTED.clear()
        k = jobs.publish_clips(j, [files[0]["n"], files[1]["n"]], ["youtube"])
        self.assertEqual(k, 2)
        self.assertTrue(wait(lambda: len(POSTED) >= 1, 30))
        st = j.clip(files[1]["n"])["published"]["youtube"]
        self.assertEqual(st["state"], "scheduled")
        meta = POSTED[0][1]
        self.assertIn("#GTA6", meta["description"]); self.assertTrue(meta["tags"])
        jobs.scheduler_tick(now=st["when"] + 1)
        self.assertTrue(wait(lambda: len(POSTED) >= 2, 30))
        # stay rate feeds the ratings
        self.assertEqual(jobs.set_stay(j, files[0]["n"], 63), 5)
        # start over and delete
        jobs.restart(j)
        self.assertTrue(wait(lambda: j.get("status") in ("done", "error")))
        self.assertTrue(jobs.delete(jid))
        self.assertFalse(j.dir.exists())


class LongVideo(unittest.TestCase):
    def test_hour_chunks(self):
        fresh_out("out_long")
        old = jobs.CHUNK, jobs.CHUNK_FROM, media.probe_url
        jobs.CHUNK, jobs.CHUNK_FROM = 60, 90
        media.probe_url = lambda u: {"ok": True, "duration": 170.0, "title": "Long", "kind": "video", "uploader": "Tester"}
        try:
            jid = jobs.create({"url": "http://127.0.0.1:8871/long.mp4", "source_type": "video", "num_clips": 3, "min_len": 15, "max_len": 30,
                               "crop_mode": "center"})
            j = jobs.JOBS[jid]
            self.assertTrue(wait(lambda: j.get("status") in ("done", "error"), 400))
            self.assertEqual(j.get("status"), "done", j.get("error"))
            self.assertEqual(sorted(j.get("chunk_state")), ["ch_000", "ch_001"])
            for c in j.data["clips"]:
                self.assertTrue(c.get("file") and c.get("chunk"))
            self.assertEqual(j.get("streamer"), "Tester")
        finally:
            jobs.CHUNK, jobs.CHUNK_FROM, media.probe_url = old


class Live(unittest.TestCase):
    def test_record_preview_manual_pause_resume(self):
        fresh_out("out_live")
        src = TMP / "hls_src"; src.mkdir()
        gen = subprocess.Popen([FF, "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=f=300",
                                "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac", "-f", "hls", "-hls_time", "2",
                                "-hls_list_size", "6", "-hls_flags", "delete_segments", str(src / "index.m3u8")])
        srv = serve(src, 8872)
        old = media.resolve_live, media.probe_url, media.PIPE_SITES
        media.resolve_live = lambda url: ([("http://127.0.0.1:8872/index.m3u8", {})], {"live_status": "is_live", "title": "sim"})
        media.probe_url = lambda u: {"title": "sim", "uploader": "SimStreamer"}
        media.PIPE_SITES = ("127.0.0.1",)  # use the yt-dlp pipe, like Twitch
        try:
            time.sleep(4)
            jid = jobs.create({"url": "http://127.0.0.1:8872/index.m3u8", "source_type": "live",
                               "live": {"record_min": 0, "chunk_min": 3, "per_chunk": 1, "review": True, "instant": False}})
            j = jobs.JOBS[jid]
            self.assertTrue(wait(lambda: jobs.live_window(j), 60), "player feed appears")
            time.sleep(20)
            now = time.time()
            n = jobs.manual_clip(j, {"wall_start": now - 12, "wall_end": now})
            self.assertTrue(wait(lambda: (j.clip(n) or {}).get("file"), 120), "manual clip renders")
            self.assertTrue(jobs.pause(j))
            self.assertTrue(wait(lambda: j.get("status") == "paused", 60))
            self.assertTrue(jobs.resume(j))
            time.sleep(15)
            j.stop.set()
            self.assertTrue(wait(lambda: j.get("status") in ("done", "error"), 240))
            self.assertEqual(j.get("status"), "done", j.get("error"))
        finally:
            media.resolve_live, media.probe_url, media.PIPE_SITES = old
            gen.kill(); srv.shutdown()


if __name__ == "__main__":
    only = sys.argv[1:]
    suite = unittest.TestSuite()
    for case in (Security, PostText, VideoProject, LongVideo, Live):
        if not only or case.__name__ in only:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(len(r.failures) + len(r.errors))
