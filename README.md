# Transcriber

Paste one or more video URLs (Instagram, YouTube, TikTok, X/Twitter, or any
site `yt-dlp` supports) — or upload files directly — and get spoken
transcripts back. Runs entirely locally:

1. `yt-dlp` downloads the video (skipped for uploaded files).
2. `ffmpeg` extracts the audio track.
3. `faster-whisper` transcribes it (model size selectable per run).
4. Hindi output can be romanized into casual Hinglish spelling.

## Setup

```bash
cd transcriber
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

(ffmpeg must be installed separately — `brew install ffmpeg` on macOS.)

## Run

```bash
./venv/bin/python app.py
```

Then open http://127.0.0.1:5050

## Features

- **Multi-platform** — not Instagram-only; any URL `yt-dlp` can resolve works.
- **Batch mode** — paste multiple URLs (one per line), each becomes its own
  job and processes independently.
- **File upload** — drag in a video/audio file you already have, no URL
  needed.
- **Live progress** — each job shows its real stage (fetching info →
  downloading → extracting audio → transcribing → romanizing) instead of a
  generic spinner.
- **Model picker** — choose `tiny`/`base`/`small`/`medium`/`large-v3` per
  run, trading speed for accuracy.
- **Editable transcript** — fix small errors inline, then "Save edit" to
  persist the correction.
- **Export** — download each transcript as `.txt`, `.srt`, or `.vtt`
  (timestamped captions).
- **History** — every completed transcript is saved to a local SQLite DB
  (`history.db`), browsable/searchable from the History tab, and
  survives server restarts. Delete old entries from there too.

## Reliability & scalability

- **Logging** - structured, timestamped logs go to both the console and a
  rotating `app.log` (2MB x 3 backups) instead of scattered `print()` calls.
- **Health check** - `GET /api/health` reports uptime, whether `ffmpeg` was
  found on PATH, which Whisper model sizes are currently loaded, and how
  many jobs are queued/running/done/errored in memory.
- **Timeouts** - `ffmpeg` extraction has a hard timeout (`FFMPEG_TIMEOUT`,
  default 120s) and yt-dlp network calls use bounded socket timeouts +
  retries, so one stuck download/extraction can't wedge a worker forever.
- **Separated concurrency** - downloading is I/O-bound and runs with
  `DOWNLOAD_WORKERS` (default 4) parallel workers; transcription is
  CPU-bound and is serialized through a semaphore
  (`TRANSCRIBE_CONCURRENCY`, default 1) so multiple Whisper models don't
  thrash each other for CPU cores. Batch jobs download in parallel but
  transcribe one at a time.
- **Bounded memory** - completed/errored jobs are pruned from the in-memory
  job store after `JOB_RETENTION_SECONDS` (default 6h); nothing is lost,
  since the result already lives in `history.db`. Orphaned upload files
  (left behind only if the server was killed mid-job) are swept on startup.
- **Resumable jobs** - the browser remembers in-flight job IDs in
  localStorage, so refreshing the page (or reopening the tab) picks the
  live progress back up instead of losing track of it.
- **Retry** - failed URL jobs (transient network errors, temporary rate
  limiting) get a one-click Retry button that resubmits with the same
  options.
- **Paginated history** - `/api/history` returns 50 rows at a time with a
  "Load more" button, so the History tab stays responsive even after
  months of use.
- **Automated tests** - `test_app.py` covers the romanization logic,
  caption formatting, and core API wiring:
  ```bash
  ./venv/bin/python -m unittest test_app.py -v
  ```

All of the above are tunable via environment variables if you want to
change the defaults: `DEFAULT_MODEL`, `DOWNLOAD_WORKERS`,
`TRANSCRIBE_CONCURRENCY`, `JOB_RETENTION_SECONDS`, `FFMPEG_TIMEOUT`,
`YTDLP_SOCKET_TIMEOUT`.

## Notes & limitations

- **Public content only by default.** Private accounts, age-restricted
  posts, or content a platform is rate-limiting for anonymous requests will
  fail to download. To use your own logged-in session, set one of these env
  vars before starting the server:
  - `COOKIES_FROM_BROWSER=chrome` (or `firefox`, `safari`, etc.) — reuses
    your browser's existing login for that site.
  - `COOKIES_FILE=/path/to/cookies.txt` — a Netscape-format cookies file.
- **Only use this on content you have the right to download/transcribe.**
  Platform Terms of Service restrict scraping content you don't own or
  don't have permission to use — this tool doesn't get you around that.
- **Mixed-language (code-switched) audio** — e.g. a sentence that mixes
  Hindi and English — is genuinely hard for Whisper, since it picks one
  language for the whole clip. Explicitly forcing "Hindi" or "English"
  (instead of Auto-detect) usually helps more than it hurts here.
- First use of a given model size downloads its weights from Hugging Face
  (e.g. `medium` is ~1.5GB) — one-time, then cached locally. `large-v3` is
  the most accurate but slow and memory-heavy on CPU.
- The server holds one loaded Whisper model per size you've used in this
  run (in memory) - if you bounce between many model sizes in one session,
  memory usage adds up. Restart the server to release it.
- `uploads/` holds uploaded files only transiently (deleted right after
  processing); `history.db` is the only thing that persists across restarts.
