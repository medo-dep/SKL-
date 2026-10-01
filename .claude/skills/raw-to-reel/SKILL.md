---
name: raw-to-reel
description: Open the Raw-to-Reel one-click video editor (local web page in Chrome) that turns a raw talking-head video into a finished vertical reel and a DaVinci Resolve timeline. Use when the user says "raw to reel", "منتج الفيديو", "افتح المونتير", or wants to edit a raw video into a reel.
---

# Raw to Reel

1. Make sure `ffmpeg` is installed (`ffmpeg -version`). If not: macOS `brew install ffmpeg`, Windows `winget install ffmpeg`.
2. For captions / filler-word / bad-take removal, make sure Whisper is installed:
   `python3 -c "import faster_whisper"` — if it fails, run `pip install faster-whisper`.
3. Start the server in the background from the repo root:
   `python3 raw-to-reel/server.py` (on Windows: `python raw-to-reel\server.py`, or double-click `start-windows.bat`)
   It opens http://127.0.0.1:4680 in the default browser (Chrome). Tell the user to pick their video(s), toggle the edits, and click "ابدأ المونتاج".
4. When the user says the edit finished, read the newest `raw-to-reel/workspace/jobs/*/result.json` and `job.json`, summarise what was cut (input vs output length, segments, and each entry in `reels`: file, seconds, thumbnail, srt), and apply any "notes" in `job.json` by re-running `python3 raw-to-reel/editor.py <inputs> --out <job dir> [--logo F] [--music F] [--broll F ...] --options '<json>'` with adjusted options (option names: `DEFAULT_OPTIONS` in `raw-to-reel/editor.py`; uploaded assets are listed in `job.json` under `assets`).
5. If DaVinci Resolve is installed and open, run `python3 raw-to-reel/workspace/jobs/<job>/resolve_import.py` to build the timeline inside Resolve. Otherwise tell the user to import `timeline.edl` via File › Import › Timeline and relink to their original clips.
