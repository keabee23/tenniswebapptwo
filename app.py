import datetime
import json
import logging
import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename

from analyzer import ContactAnalyzer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
RUNS_DIR = BASE_DIR / "runs"
UPLOAD_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

RUN_TTL_SECONDS = 24 * 60 * 60  # purge runs older than 24 hours
HISTORY_LIMIT = 50               # max runs returned by /history

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "30 per hour"],
    storage_uri="memory://",
)

try:
    analyzer = ContactAnalyzer(str(RUNS_DIR), model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"))
except RuntimeError as exc:
    logger.critical("Failed to initialize ContactAnalyzer: %s", exc)
    raise


# ── Helpers ────────────────────────────────────────────────────────────────────

def purge_old_runs() -> None:
    """Delete run directories older than RUN_TTL_SECONDS."""
    cutoff = time.time() - RUN_TTL_SECONDS
    for run_dir in RUNS_DIR.iterdir():
        if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
            try:
                shutil.rmtree(run_dir)
                logger.info("Purged old run: %s", run_dir.name)
            except OSError as exc:
                logger.warning("Could not purge run %s: %s", run_dir.name, exc)


def _safe_filename(filename: str):
    """Return sanitised filename or None if it contains path traversal."""
    safe_parts = [p for p in Path(filename).parts if p not in ("", ".", "..")]
    if len(safe_parts) != len(Path(filename).parts) or not safe_parts:
        return None
    result = str(Path(*safe_parts))
    return None if ".." in result else result


def _write_result_json(run_id: str, result: dict, video_filename: str, duration: float) -> None:
    """Persist a summary of the analysis result for the /history and /runs/<id>/result endpoints."""
    result_path = RUNS_DIR / run_id / "result.json"
    data = {
        "run_id": run_id,
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "video_filename": video_filename,
        "fps": result.get("fps"),
        "frame_count": result.get("frame_count"),
        "status": result.get("status"),
        "contact_frame": result.get("contact_frame"),
        "confidence": result.get("confidence"),
        "serve_confirmed": result.get("serve_confirmed"),
        "contact_confirmed": result.get("contact_confirmed"),
        "reason": result.get("reason", ""),
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        "duration_seconds": round(duration, 2),
    }
    try:
        result_path.write_text(json.dumps(data))
    except OSError as exc:
        logger.warning("Could not write result.json for run %s: %s", run_id, exc)


def _parse_float_param(name: str, low: float = 0.0, high: float = 1.0) -> float | None:
    """Parse an optional float form field, clamped to [low, high]. Returns None if absent/invalid."""
    raw = request.form.get(name)
    if raw is None:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return max(low, min(high, val))


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
@limiter.limit("10 per hour")
def analyze_video():
    purge_old_runs()

    if "video" not in request.files:
        return jsonify({"error": "No video file was uploaded."}), 400

    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "Please choose a video file."}), 400

    if not analyzer.allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Use mp4, mov, m4v, avi, mpg, or mpeg."}), 400

    # Optional serve mark (seconds) — skips Phase 0 when provided
    serve_mark = _parse_float_param("serve_mark", low=0.0, high=3600.0)

    # Optional crop region (0.0–1.0 fractions)
    crop_left   = _parse_float_param("crop_left")
    crop_right  = _parse_float_param("crop_right")
    crop_top    = _parse_float_param("crop_top")
    crop_bottom = _parse_float_param("crop_bottom")

    crop_kwargs = {}
    if crop_left   is not None: crop_kwargs["crop_left"]   = crop_left
    if crop_right  is not None: crop_kwargs["crop_right"]  = crop_right
    if crop_top    is not None: crop_kwargs["crop_top"]    = crop_top
    if crop_bottom is not None: crop_kwargs["crop_bottom"] = crop_bottom

    filename = secure_filename(file.filename)
    video_path = UPLOAD_DIR / filename
    file.save(video_path)
    logger.info("Upload received: %s (%.1f MB)", filename, video_path.stat().st_size / 1_048_576)

    start_time = time.time()
    try:
        result = analyzer.analyze(str(video_path), serve_mark_seconds=serve_mark, **crop_kwargs)
    except (RuntimeError, ValueError, OSError) as exc:
        logger.error("Analysis failed for %s: %s", filename, exc)
        return jsonify({"error": "Analysis failed. Please try a different video."}), 500
    finally:
        try:
            video_path.unlink(missing_ok=True)
        except OSError:
            pass

    duration = time.time() - start_time
    status = result.get("status")
    run_id = result.get("run_id")

    if run_id:
        _write_result_json(run_id, result, filename, duration)

    if status == "not_a_serve":
        return jsonify({
            "status": "not_a_serve",
            "reason": result.get("reason", "No serve motion was detected in this video."),
            "run_id": run_id,
        })

    if status == "found":
        payload = {
            "status": "found",
            "before_frame": result["before_frame"],
            "contact_frame": result["contact_frame"],
            "after_frame": result["after_frame"],
            "reason": result.get("reason", ""),
            "confidence": result.get("confidence", "medium"),
            "serve_confirmed": result.get("serve_confirmed"),
            "contact_confirmed": result.get("contact_confirmed"),
            "before_url": url_for("serve_run_file", run_id=run_id, filename=f"output/frame_{result['before_frame']}.png"),
            "after_url": url_for("serve_run_file", run_id=run_id, filename=f"output/frame_{result['after_frame']}.png"),
            "triplet_url": url_for("serve_run_file", run_id=run_id, filename="output/triplet.png"),
            "triplet_zoom_url": url_for("serve_run_file", run_id=run_id, filename="output/triplet_zoom.png"),
            "contact_url": url_for("serve_run_file", run_id=run_id, filename=f"output/frame_{result['contact_frame']}.png"),
            "contact_zoom_url": url_for("serve_run_file", run_id=run_id, filename=f"output/frame_{result['contact_frame']}_zoom.png"),
            "contact_sheet_url": url_for("serve_run_file", run_id=run_id, filename="output/contact_sheet.png"),
            "fps": result.get("fps"),
            "frame_count": result.get("frame_count"),
        }
        return jsonify(payload)

    # indeterminate
    return jsonify({
        "status": result.get("status", "indeterminate"),
        "reason": result.get("reason", "Could not determine the first-contact frame."),
        "serve_confirmed": result.get("serve_confirmed"),
        "contact_confirmed": result.get("contact_confirmed"),
        "run_id": run_id,
        "contact_sheet_url": url_for("serve_run_file", run_id=run_id, filename="output/contact_sheet.png") if run_id else None,
    })


@app.route("/runs/<run_id>/result")
def run_result(run_id: str):
    """Return the audit-trail JSON for a completed run."""
    result_path = RUNS_DIR / run_id / "result.json"
    if not result_path.exists():
        return jsonify({"error": "Run not found."}), 404
    try:
        data = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read result.json for run %s: %s", run_id, exc)
        return jsonify({"error": "Could not read run result."}), 500
    return jsonify(data)


@app.route("/history")
def history():
    """Return the last N runs, newest-first, each with summary fields."""
    runs = []
    for run_dir in sorted(RUNS_DIR.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
        if not run_dir.is_dir():
            continue
        result_path = run_dir / "result.json"
        if not result_path.exists():
            continue
        try:
            data = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["contact_sheet_url"] = url_for(
            "serve_run_file", run_id=run_dir.name, filename="output/contact_sheet.png"
        )
        runs.append(data)
        if len(runs) >= HISTORY_LIMIT:
            break
    return jsonify(runs)


@app.route("/runs/<run_id>/<path:filename>")
def serve_run_file(run_id: str, filename: str):
    safe = _safe_filename(filename)
    if safe is None:
        return jsonify({"error": "Invalid path."}), 400
    return send_from_directory(RUNS_DIR / run_id, safe)


if __name__ == "__main__":
    app.run(debug=False)
