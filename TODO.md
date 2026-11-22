# To fix later

Done on Nov 22, 2026 (from the audit): local server token + Origin/Host check, DPAPI-encrypted keys and tokens,
"Copy diagnostics", update check, automated tests (`python tests/run_tests.py`, run before every push), disk-space guard
with "Free up space", backup/restore of ratings and settings, deleting a project stops its chat download.

Still open:
- Check campaign rules (length, watermark, credit) before publishing.
- Verify checksums of Python/ffmpeg downloaded on first run.
- Frame judging only re-ranks moments the transcript/chat/loudness pass found; silent moments it missed stay missed.
- YouTube's exact "stayed to watch" (viewed vs swiped away) isn't in the Analytics API; the daily check uses average % viewed
  and views instead. The typed-in stay rate still works on top of it.
