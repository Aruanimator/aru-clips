# To fix later (from the Nov 22, 2026 audit)

Not started yet. In priority order:

1. **Local server safety:** require a random session token from the UI, and check the Origin/Host headers on every API call (blocks webpages and DNS rebinding from reaching `127.0.0.1`).
2. **Encrypt secrets:** store OAuth tokens (`accounts.json`) and API keys (settings) with Windows DPAPI instead of plain JSON.
3. **"Copy diagnostics" button:** version, recent project logs and recorder logs in one click, for bug reports.
4. **Update check at launch:** tell the user when a newer `AruClips.exe` is on GitHub.
5. **Automated tests:** turn the sandbox harnesses (pause/resume, live reconnect, Twitch pipe, hour chunks, publishing, scheduling) into a test suite and run it before every push.
6. **Disk-space guard:** warn when the clips drive is low; offer to delete source videos of finished projects.

Also noted:
- Check campaign rules (length, watermark, credit) before publishing.
- Verify checksums of Python/ffmpeg downloaded on first run.
- Back up ratings and settings.
- Deleting a project should stop its chat-replay download first (deleted projects leave large `chat.live_chat.json.part` files behind).
