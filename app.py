import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from analyzer import ContactAnalyzer

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
RUNS_DIR = BASE_DIR / "runs"
UPLOAD_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)

analyzer = ContactAnalyzer(str(RUNS_DIR), model=os.getenv("OPENAI_MODEL", "gpt-5"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze_video():
    if "video" not in request.files:
        return jsonify({"error": "No video file was uploaded."}), 400

    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "Please choose a video file."}), 400

    if not analyzer.allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Use mp4, mov, m4v, avi, mpg, or mpeg."}), 400

    filename = secure_filename(file.filename)
    video_path = UPLOAD_DIR / filename
    file.save(video_path)

    try:
        result = analyzer.analyze(str(video_path))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if result.get("status") == "found":
        artifacts = result.get("artifacts", {})
        payload = {
            "status": result["status"],
            "before_frame": result["before_frame"],
            "contact_frame": result["contact_frame"],
            "after_frame": result["after_frame"],
            "reason": result.get("reason", ""),
            "confidence": result.get("confidence", "medium"),
            "triplet_url": url_for("serve_run_file", run_id=result["run_id"], filename="output/triplet.png"),
            "triplet_zoom_url": url_for("serve_run_file", run_id=result["run_id"], filename="output/triplet_zoom.png"),
            "contact_url": url_for("serve_run_file", run_id=result["run_id"], filename=f"output/frame_{result['contact_frame']}.png"),
            "contact_zoom_url": url_for("serve_run_file", run_id=result["run_id"], filename=f"output/frame_{result['contact_frame']}_zoom.png"),
            "contact_sheet_url": url_for("serve_run_file", run_id=result["run_id"], filename="output/contact_sheet.png"),
            "fps": result.get("fps"),
            "frame_count": result.get("frame_count"),
        }
        return jsonify(payload)

    return jsonify({
        "status": result.get("status", "indeterminate"),
        "reason": result.get("reason", "Could not determine the first-contact frame."),
        "run_id": result.get("run_id"),
        "contact_sheet_url": url_for("serve_run_file", run_id=result["run_id"], filename="output/contact_sheet.png") if result.get("run_id") else None,
    })


@app.route("/runs/<run_id>/<path:filename>")
def serve_run_file(run_id: str, filename: str):
    return send_from_directory(RUNS_DIR / run_id, filename)


if __name__ == "__main__":
    app.run(debug=True)
