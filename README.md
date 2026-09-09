# Transcriber

Local-first web app for downloading media audio and generating editable transcripts.

## Overview

Transcriber accepts supported video URLs (YouTube, Instagram, TikTok, X/Twitter, and more via `yt-dlp`) or direct file uploads, extracts audio with `ffmpeg`, and transcribes speech with `faster-whisper`. It also supports Hindi romanization, transcript export, and local transcript history.

## Key Features

- URL-based and direct file-upload transcription flows
- Batch processing for multiple URLs
- Per-run Whisper model selection (`tiny` to `large-v3`)
- Live job progress with retry support
- Editable transcripts with export to `.txt`, `.srt`, and `.vtt`
- Local history storage in SQLite (`history.db`)
- Health endpoint, structured logging, and configurable worker/timeouts

## Tech Stack

- Python
- Flask (web/API server)
- `faster-whisper` (speech-to-text)
- `yt-dlp` (media retrieval)
- `ffmpeg` (audio extraction)
- SQLite (history persistence)

## Setup & Run

1. Install `ffmpeg` on your system (example on macOS: `brew install ffmpeg`).
2. Create and activate a virtual environment, then install dependencies:

```bash
cd /home/runner/work/Transcriber/Transcriber
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

3. Start the app:

```bash
./venv/bin/python app.py
```

4. Open: http://127.0.0.1:5050

## Usage

1. Paste one or more supported URLs, or upload an audio/video file.
2. Choose transcription options (including model size).
3. Wait for processing to complete and review/edit the transcript.
4. Export output (`.txt`, `.srt`, `.vtt`) or find prior items in History.

## Project Structure

- `app.py` — main Flask application and API logic
- `templates/` — web UI templates
- `requirements.txt` — Python dependencies
- `test_app.py` — unit tests
- `launchd/` — optional macOS service files

## Contribution

Contributions are welcome through focused pull requests with a clear description and test updates where applicable.

## License / Contact

No repository license file is currently included. For ownership or usage questions, contact the repository owner via GitHub: `@sidddharthhahir`.
