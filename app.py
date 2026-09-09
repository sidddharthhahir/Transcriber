import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300MB, generous for a video file

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "history.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "medium")
DOWNLOAD_WORKERS = int(os.environ.get("DOWNLOAD_WORKERS", "4"))
TRANSCRIBE_CONCURRENCY = int(os.environ.get("TRANSCRIBE_CONCURRENCY", "1"))
JOB_RETENTION_SECONDS = int(os.environ.get("JOB_RETENTION_SECONDS", str(6 * 3600)))
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "120"))
YTDLP_SOCKET_TIMEOUT = int(os.environ.get("YTDLP_SOCKET_TIMEOUT", "30"))

logger = logging.getLogger("transcriber")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
_file_handler = RotatingFileHandler(BASE_DIR / "app.log", maxBytes=2_000_000, backupCount=3)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)
logger.addHandler(_console_handler)

if not shutil.which("ffmpeg"):
    logger.error("ffmpeg not found on PATH - every job will fail at the audio-extraction "
                  "step. Install it (e.g. `brew install ffmpeg`) and restart.")


_models = {}
_models_lock = threading.Lock()
_transcribe_semaphore = threading.Semaphore(TRANSCRIBE_CONCURRENCY)

MODEL_SIZES = {"tiny", "base", "small", "medium", "large-v3"}


def get_model(size: str = "medium"):
    if size not in MODEL_SIZES:
        size = DEFAULT_MODEL
    if size not in _models:
        with _models_lock:
            if size not in _models:  # re-check: another thread may have won the race
                from faster_whisper import WhisperModel
                logger.info(f"Loading Whisper model '{size}'…")
                _models[size] = WhisperModel(size, device="cpu", compute_type="int8")
                logger.info(f"Whisper model '{size}' ready.")
    return _models[size]


_ROMAN_VOWELS = set("aeiouAEIOU")


def _strip_word_final_schwa(word: str) -> str:
    """Approximate Hindi schwa deletion in romanized output."""
    if len(word) < 2 or word[-1] != "a":
        return word
    if word[-2] in _ROMAN_VOWELS:
        return word  # preceded by a real vowel (e.g. explicit ā) - keep it
    candidate = word[:-1]
    if not any(c in _ROMAN_VOWELS for c in candidate):
        return word  # would leave an unpronounceable consonant cluster
    return candidate


def devanagari_to_roman(text: str) -> str:
    """Transliterate Devanagari (Hindi script) text into casual Roman/Hinglish
    spelling, e.g. 'अभी कैसे हो बढ़िया सब' -> 'abhi kaise ho badhiya sab'."""
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate

    itrans = transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)

    def process_token(tok: str) -> str:
        if not tok or not tok[0].isalpha():
            return tok
        tok = tok.replace("M", "n")  # anusvara (ं) -> casual "n"
        tok = _strip_word_final_schwa(tok)
        tok = tok.replace(".", "")  # ITRANS nukta/diacritic marker
        return tok.lower()

    tokens = re.findall(r"[A-Za-z.]+|[^A-Za-z.]+", itrans)
    return "".join(process_token(t) for t in tokens)


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _run(fn):
    conn = _db()
    try:
        with conn:
            return fn(conn)
    finally:
        conn.close()


def init_db():
    _run(lambda conn: conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY,
            source_type TEXT,
            source TEXT,
            title TEXT,
            thumbnail TEXT,
            language TEXT,
            romanized INTEGER,
            model TEXT,
            duration REAL,
            transcript TEXT,
            segments TEXT,
            created_at REAL
        )
    """))
    _run(lambda conn: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_created_at ON history(created_at DESC)"
    ))


init_db()


def save_history(record: dict):
    _run(lambda conn: conn.execute(
        """INSERT OR REPLACE INTO history
           (id, source_type, source, title, thumbnail, language, romanized,
            model, duration, transcript, segments, created_at)
           VALUES (:id, :source_type, :source, :title, :thumbnail, :language,
                   :romanized, :model, :duration, :transcript, :segments, :created_at)""",
        record,
    ))


def list_history(limit: int = 50, offset: int = 0):
    rows = _run(lambda conn: conn.execute(
        "SELECT id, source_type, source, title, thumbnail, language, romanized, "
        "model, duration, created_at, substr(transcript, 1, 180) AS snippet "
        "FROM history ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall())
    return [dict(r) for r in rows]


def count_history() -> int:
    row = _run(lambda conn: conn.execute("SELECT COUNT(*) AS c FROM history").fetchone())
    return row["c"]


def get_history_row(hid: str):
    row = _run(lambda conn: conn.execute("SELECT * FROM history WHERE id = ?", (hid,)).fetchone())
    return dict(row) if row else None


def update_history_transcript(hid: str, transcript: str) -> bool:
    cur = _run(lambda conn: conn.execute("UPDATE history SET transcript = ? WHERE id = ?", (transcript, hid)))
    return cur.rowcount > 0


def delete_history_row(hid: str) -> bool:
    cur = _run(lambda conn: conn.execute("DELETE FROM history WHERE id = ?", (hid,)))
    return cur.rowcount > 0


class DownloadError(Exception):
    pass


_jobs = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)


def new_job(kind: str, source: str, options: dict) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "source": source,
        "options": options,
        "status": "queued",
        "stage": "Queued…",
        "error": None,
        "preview": None,
        "result": None,
        "created_at": time.time(),
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
    return job


def update_job(job_id: str, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _prune_old_jobs():
    while True:
        time.sleep(600)
        cutoff = time.time() - JOB_RETENTION_SECONDS
        with _jobs_lock:
            stale = [jid for jid, j in _jobs.items()
                     if j["status"] in ("done", "error") and j["created_at"] < cutoff]
            for jid in stale:
                del _jobs[jid]
        if stale:
            logger.info(f"Pruned {len(stale)} stale in-memory job(s) (results remain in history.db).")


def _cleanup_orphaned_uploads():
    """Remove old uploads left behind by interrupted jobs."""
    now = time.time()
    for f in UPLOAD_DIR.glob("*"):
        try:
            if now - f.stat().st_mtime > 3600:
                f.unlink()
                logger.info(f"Removed orphaned upload: {f.name}")
        except OSError:
            pass


_cleanup_orphaned_uploads()
threading.Thread(target=_prune_old_jobs, daemon=True, name="job-pruner").start()


def platform_label(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    return host or "video"


def run_job(job_id: str, file_path: str | None = None):
    job = get_job(job_id)
    options = job["options"]
    update_job(job_id, status="running", stage="Starting…")
    work_dir = tempfile.mkdtemp(prefix="reel_")
    try:
        if job["kind"] == "url":
            url = job["source"]
            update_job(job_id, stage="Fetching video info…")
            preview = fetch_preview(url)
            if preview:
                update_job(job_id, preview=preview)
            update_job(job_id, stage="Downloading…")
            video_path = download_media(url, work_dir)
        else:
            video_path = file_path
            update_job(job_id, preview={
                "title": job["source"],
                "thumbnail": None,
                "platform": "uploaded file",
            })

        update_job(job_id, stage="Extracting audio…")
        audio_path = extract_audio(video_path, work_dir)

        model_size = options.get("model", DEFAULT_MODEL)
        update_job(job_id, stage=f"Transcribing (Whisper {model_size})…")
        language = options.get("language")
        segments, info = run_transcription(
            audio_path,
            language=None if language in (None, "auto") else language,
            model_size=model_size,
        )

        detected_lang = info["language"]
        romanized = False
        if options.get("romanize", True) and detected_lang == "hi":
            update_job(job_id, stage="Romanizing Hindi…")
            for seg in segments:
                seg["text"] = devanagari_to_roman(seg["text"])
            romanized = True

        transcript_text = " ".join(seg["text"].strip() for seg in segments).strip()

        preview = get_job(job_id).get("preview") or {}
        result = {
            "transcript": transcript_text,
            "segments": segments,
            "language": detected_lang,
            "romanized": romanized,
            "model": model_size,
            "duration": info["duration"],
            "title": preview.get("title") or job["source"],
            "thumbnail": preview.get("thumbnail"),
        }

        save_history({
            "id": job_id,
            "source_type": job["kind"],
            "source": job["source"],
            "title": result["title"],
            "thumbnail": result["thumbnail"],
            "language": detected_lang,
            "romanized": int(romanized),
            "model": model_size,
            "duration": info["duration"],
            "transcript": transcript_text,
            "segments": json.dumps(segments),
            "created_at": time.time(),
        })

        update_job(job_id, status="done", stage="Done", result=result)
        logger.info(f"Job {job_id} done ({job['kind']}: {job['source']!r}, {model_size}, {detected_lang}).")
    except DownloadError as e:
        update_job(job_id, status="error", stage="Failed", error=str(e))
        logger.warning(f"Job {job_id} failed ({job['kind']}: {job['source']!r}): {e}")
    except Exception as e:
        logger.exception(f"Job {job_id} hit an unexpected error ({job['kind']}: {job['source']!r})")
        update_job(job_id, status="error", stage="Failed", error=f"Unexpected error: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass


def _ydl_opts(**extra):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
    }
    cookies_browser = os.environ.get("COOKIES_FROM_BROWSER")
    cookies_file = os.environ.get("COOKIES_FILE")
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    elif cookies_file:
        opts["cookiefile"] = cookies_file
    opts.update(extra)
    return opts


def fetch_preview(url: str):
    """Fetch title/thumbnail metadata without failing job creation."""
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title") or (info.get("description") or "")[:80] or url,
            "thumbnail": info.get("thumbnail"),
            "platform": platform_label(url),
        }
    except Exception as e:
        logger.info(f"fetch_preview failed for {url}: {e}")
        return {"title": url, "thumbnail": None, "platform": platform_label(url)}


def download_media(url: str, work_dir: str) -> str:
    import yt_dlp

    out_template = os.path.join(work_dir, "media.%(ext)s")
    ydl_opts = _ydl_opts(outtmpl=out_template, format="mp4/bestaudio/best")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.warning(f"yt-dlp failed for {url}: {e}")
        raise DownloadError(
            "Couldn't download this video. It's likely private, age-restricted, "
            "or the platform is blocking anonymous requests for this post "
            "(some public-looking content still requires a logged-in session), "
            "or the URL isn't from a site yt-dlp supports."
        )

    for fname in os.listdir(work_dir):
        if fname.startswith("media."):
            return os.path.join(work_dir, fname)

    raise DownloadError("Download finished but no file was found.")


def extract_audio(video_path: str, work_dir: str) -> str:
    audio_path = os.path.join(work_dir, "audio.wav")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-ac", "1", "-ar", "16000", "-vn",
        audio_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DownloadError(
            f"Audio extraction timed out after {FFMPEG_TIMEOUT}s - the source file "
            "may be unusually long or corrupted."
        )
    except FileNotFoundError:
        raise DownloadError("ffmpeg isn't installed or isn't on PATH.")
    if result.returncode != 0 or not os.path.exists(audio_path):
        raise DownloadError(f"Failed to extract audio: {result.stderr[-500:]}")
    return audio_path


def run_transcription(audio_path: str, language: str | None = None, model_size: str = "medium"):
    model = get_model(model_size)
    # Decoder work happens during iteration, so hold the semaphore while
    # consuming segments_iter to enforce concurrency limits.
    with _transcribe_semaphore:
        segments_iter, info = model.transcribe(
            audio_path,
            beam_size=5,
            language=language,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            no_repeat_ngram_size=3,
        )
        segments = [
            {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text}
            for seg in segments_iter
        ]
        result_info = {"language": info.language, "duration": info.duration}

    return segments, result_info


def _srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def to_srt(segments: list) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(seg['start'])} --> {_srt_timestamp(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def to_vtt(segments: list) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_vtt_timestamp(seg['start'])} --> {_vtt_timestamp(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)


_start_time = time.time()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    with _jobs_lock:
        job_counts = {}
        for j in _jobs.values():
            job_counts[j["status"]] = job_counts.get(j["status"], 0) + 1
    return jsonify({
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "ffmpeg_found": shutil.which("ffmpeg") is not None,
        "models_loaded": sorted(_models.keys()),
        "jobs_in_memory": job_counts,
        "default_model": DEFAULT_MODEL,
        "download_workers": DOWNLOAD_WORKERS,
        "transcribe_concurrency": TRANSCRIBE_CONCURRENCY,
    })


def _parse_options(source: dict) -> dict:
    return {
        "language": source.get("language", "auto"),
        "romanize": source.get("romanize", True) in (True, "true", "1", 1),
        "model": source.get("model", DEFAULT_MODEL),
    }


@app.route("/api/jobs", methods=["POST"])
def create_jobs():
    job_ids = []

    if request.files:
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files received."}), 400
        options = _parse_options(request.form)
        for f in files:
            if not f.filename:
                continue
            safe_name = secure_filename(f.filename) or "upload"
            saved_path = str(UPLOAD_DIR / f"{uuid.uuid4().hex[:12]}_{safe_name}")
            f.save(saved_path)
            job = new_job("file", f.filename, options)
            job_ids.append(job["id"])
            _executor.submit(run_job, job["id"], saved_path)
    else:
        data = request.get_json(silent=True) or {}
        urls = data.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            return jsonify({"error": "Please provide at least one URL."}), 400

        bad = [u for u in urls if not re.match(r"^https?://", u)]
        if bad:
            return jsonify({"error": f"Not a valid URL: {bad[0]}"}), 400

        options = _parse_options(data)
        for url in urls:
            job = new_job("url", url, options)
            job_ids.append(job["id"])
            _executor.submit(run_job, job["id"])

    return jsonify({"job_ids": job_ids})


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job id."}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job id."}), 404
    if job["kind"] != "url":
        return jsonify({"error": "Only URL jobs can be retried - uploaded files aren't kept after processing."}), 400
    new = new_job("url", job["source"], job["options"])
    _executor.submit(run_job, new["id"])
    return jsonify({"job_id": new["id"]})


@app.route("/api/history")
def history_list():
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    return jsonify({
        "items": list_history(limit, offset),
        "total": count_history(),
        "limit": limit,
        "offset": offset,
    })


@app.route("/api/history/<hid>")
def history_get(hid):
    row = get_history_row(hid)
    if not row:
        abort(404)
    row["segments"] = json.loads(row["segments"] or "[]")
    return jsonify(row)


@app.route("/api/history/<hid>", methods=["PATCH"])
def history_patch(hid):
    data = request.get_json(silent=True) or {}
    transcript = data.get("transcript")
    if transcript is None:
        return jsonify({"error": "Missing 'transcript'."}), 400
    if not update_history_transcript(hid, transcript):
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/history/<hid>", methods=["DELETE"])
def history_delete(hid):
    if not delete_history_row(hid):
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/history/<hid>/export.<fmt>")
def history_export(hid, fmt):
    row = get_history_row(hid)
    if not row:
        abort(404)

    if fmt == "txt":
        content, mimetype = row["transcript"], "text/plain"
    elif fmt == "srt":
        content, mimetype = to_srt(json.loads(row["segments"] or "[]")), "application/x-subrip"
    elif fmt == "vtt":
        content, mimetype = to_vtt(json.loads(row["segments"] or "[]")), "text/vtt"
    else:
        abort(400)

    safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", (row["title"] or "transcript"))[:60] or "transcript"
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.{fmt}"'},
    )


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_e):
    return jsonify({"error": "File too large (300MB limit)."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    # Keep debug/reloader off by default for long-running local service use.
    debug = os.environ.get("FLASK_DEBUG") == "1"
    logger.info(f"Starting Transcriber on port {port} "
                f"(default_model={DEFAULT_MODEL}, download_workers={DOWNLOAD_WORKERS}, "
                f"transcribe_concurrency={TRANSCRIBE_CONCURRENCY}, debug={debug})")
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)
