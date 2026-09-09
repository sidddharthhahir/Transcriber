# Transcriber

Transcriber is a local Flask app that downloads media audio and produces editable transcripts with Whisper.

## What it does

- Transcribes supported video URLs via `yt-dlp` (YouTube, Instagram, TikTok, X/Twitter, and others).
- Transcribes uploaded audio/video files.
- Supports batch URL submissions.
- Lets users choose Whisper model size per job (`tiny` → `large-v3`).
- Shows per-job progress and retry for failed URL jobs.
- Stores completed transcripts in local SQLite history (`history.db`).
- Exports transcripts as `.txt`, `.srt`, and `.vtt`.
- Romanizes Hindi (Devanagari → casual Latin script) when enabled.

## Tech stack

- Python + Flask
- `faster-whisper`
- `yt-dlp`
- `ffmpeg`
- SQLite

## Run locally

1. Install `ffmpeg`.
2. Create a virtualenv and install dependencies:

```bash
cd /home/runner/work/Transcriber/Transcriber
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

3. Start the server:

```bash
./venv/bin/python app.py
```

4. Open http://127.0.0.1:5050.

## Configuration

Environment variables used by `app.py`:

- `PORT` (default: `5050`)
- `FLASK_DEBUG` (`1` enables Flask debug/reloader)
- `DEFAULT_MODEL` (default: `medium`)
- `DOWNLOAD_WORKERS` (default: `4`)
- `TRANSCRIBE_CONCURRENCY` (default: `1`)
- `JOB_RETENTION_SECONDS` (default: `21600`)
- `FFMPEG_TIMEOUT` (default: `120`)
- `YTDLP_SOCKET_TIMEOUT` (default: `30`)
- `COOKIES_FROM_BROWSER` or `COOKIES_FILE` (optional `yt-dlp` auth helpers)

## Tests

```bash
./venv/bin/python -m unittest test_app.py -v
```

## Repository layout

- `app.py` — Flask app, background jobs, API endpoints
- `templates/index.html` — single-page UI
- `test_app.py` — smoke tests
- `launchd/` — optional macOS launchd service files
