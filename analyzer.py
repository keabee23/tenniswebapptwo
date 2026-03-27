import base64
import json
import logging
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import cv2
from PIL import Image, ImageDraw, UnidentifiedImageError

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"mp4", "mov", "m4v", "avi", "mpeg", "mpg"}
SUPPORTED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

# ── Prompts ────────────────────────────────────────────────────────────────────

SERVE_DETECTION_PROMPT = """
You are a tennis expert reviewing full-frame video thumbnails to determine whether a tennis serve occurs.

A tennis serve has ALL of the following visual markers:
- Player stands sideways or at an angle to the net (not facing it head-on)
- Ball is tossed upward with one hand while the racket arm is drawn back or lifted
- Racket reaches a cocked/trophy position high behind the head
- Player extends fully upward to contact the ball above their head
- The ball-racket contact happens at the highest reachable point

This is NOT a serve:
- A groundstroke (forehand/backhand) — player faces the net, contact is at waist or shoulder height
- A volley — contact is in front of the body at net height
- A ball bounce or drop
- Practice swings without a ball

Review the provided thumbnails sampled from the full video.

Return strict JSON only (no markdown, no extra text):
{
  "serve_detected": true | false,
  "estimated_window_start": <int | null>,
  "estimated_window_end": <int | null>,
  "reason": "brief explanation"
}

If a serve is detected, set estimated_window_start/end to the frame range most likely to contain the contact.
If no serve is detected, set both to null.
""".strip()

WINDOW_PROMPT = """
You are a tennis expert narrowing down which frame window contains the ball-racket contact of a tennis serve.

You will see strips of frames — each strip shows several consecutive frames from the video.
For each strip you receive both the full frame (to verify serve body mechanics) and a zoomed crop of the upper contact zone.

Look for:
- The serve swing reaching its apex (racket fully extended upward)
- Ball descending into the strike zone just before contact
- The moment of maximum arm extension

Return strict JSON only (no markdown, no extra text):
{
  "window_start": <int>,
  "window_end": <int>,
  "reason": "brief explanation"
}
""".strip()

CONTACT_PROMPT = """
You are a tennis expert identifying the exact first-contact frame of a tennis serve.

You will see 3-frame triplets: [before, candidate, after].
The images are zoomed crops of the upper-body contact zone.

You must confirm BOTH conditions to return "found":
  (A) SERVE CONFIRMED — the motion is clearly an overhead serve strike (not a groundstroke, volley, or other shot). The racket is extended fully upward. The player's body shows serve mechanics.
  (B) CONTACT CONFIRMED — this is the precise first frame where ball touches strings. The frame before shows no contact. The frame after shows ball compressing or departing.

If you can confirm (A) but not pinpoint (B), return "indeterminate" with reason "serve confirmed but contact frame unclear".
If you can see contact in (B) but the motion does not look like a serve, return "not_a_serve".
If evidence is insufficient for either, return "indeterminate".
Do not guess. Prefer indeterminate over a wrong answer.

Return strict JSON only (no markdown, no extra text):
{
  "status": "found" | "indeterminate" | "not_a_serve",
  "before_frame": <int | null>,
  "contact_frame": <int | null>,
  "after_frame": <int | null>,
  "serve_confirmed": true | false,
  "contact_confirmed": true | false,
  "reason": "brief explanation",
  "confidence": "high" | "medium" | "low"
}
""".strip()


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class AnalysisArtifacts:
    run_id: str
    run_dir: Path
    frames_dir: Path
    crops_dir: Path
    output_dir: Path


# ── Analyzer ───────────────────────────────────────────────────────────────────

class ContactAnalyzer:
    def __init__(self, runs_root: str, model: str = "claude-sonnet-4-20250514"):
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.model = model
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "The application cannot start without it."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    @staticmethod
    def allowed_file(filename: str) -> bool:
        return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

    def prepare_run(self) -> AnalysisArtifacts:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self.runs_root / run_id
        frames_dir = run_dir / "frames"
        crops_dir = run_dir / "crops"
        output_dir = run_dir / "output"
        frames_dir.mkdir(parents=True, exist_ok=True)
        crops_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        return AnalysisArtifacts(run_id, run_dir, frames_dir, crops_dir, output_dir)

    # ── Main entry point ───────────────────────────────────────────────────────

    def analyze(
        self,
        video_path: str,
        serve_mark_seconds: Optional[float] = None,
        crop_left: float = 0.28,
        crop_right: float = 0.72,
        crop_top: float = 0.03,
        crop_bottom: float = 0.55,
    ) -> Dict[str, Any]:
        artifacts = self.prepare_run()
        logger.info("Run %s | extracting frames from %s", artifacts.run_id, video_path)

        meta = self.extract_all_frames(video_path, artifacts, crop_left, crop_right, crop_top, crop_bottom)
        frame_count = meta["frame_count"]

        if frame_count < 3:
            logger.warning("Run %s | too few frames (%d)", artifacts.run_id, frame_count)
            return {
                "status": "indeterminate",
                "reason": "Video has fewer than 3 readable frames.",
                "run_id": artifacts.run_id,
            }

        # Phase 0: confirm a serve exists and get rough location.
        # Skipped when caller supplies a serve_mark_seconds hint.
        if serve_mark_seconds is not None:
            center = int(serve_mark_seconds * meta["fps"])
            search_start = max(0, center - 30)
            search_end = min(frame_count - 1, center + 30)
            logger.info("Run %s | serve_mark=%.2fs → search [%d–%d]", artifacts.run_id, serve_mark_seconds, search_start, search_end)
        else:
            logger.info("Run %s | phase 0: serve detection", artifacts.run_id)
            serve_meta = self.detect_serve_presence(frame_count, artifacts)
            if not serve_meta.get("serve_detected"):
                logger.info("Run %s | no serve detected: %s", artifacts.run_id, serve_meta.get("reason"))
                return {
                    "status": "not_a_serve",
                    "reason": serve_meta.get("reason", "No serve motion detected in this video."),
                    "run_id": artifacts.run_id,
                }

            # Honour the serve-detection window hint if valid, else scan all frames
            hint_start = serve_meta.get("estimated_window_start")
            hint_end = serve_meta.get("estimated_window_end")
            if (
                isinstance(hint_start, int)
                and isinstance(hint_end, int)
                and 0 <= hint_start < hint_end < frame_count
            ):
                search_start, search_end = hint_start, hint_end
            else:
                search_start, search_end = 0, frame_count - 1

        # Phase 1: narrow to best window using full + zoomed frames
        logger.info("Run %s | phase 1: window selection [%d–%d]", artifacts.run_id, search_start, search_end)
        first_pass = self.pick_candidate_windows(search_start, search_end, frame_count)
        shortlist = self.ask_model_for_window(first_pass, artifacts)
        window = self.coerce_window(shortlist, frame_count) or (search_start, search_end)

        # Phase 2: find exact contact triplet with dual-condition gate
        logger.info("Run %s | phase 2: contact triplet [%d–%d]", artifacts.run_id, window[0], window[1])
        refined_groups = self.expand_groups(*window, frame_count=frame_count)
        decision = self.ask_model_for_triplet(refined_groups, artifacts)

        if decision.get("status") == "not_a_serve":
            logger.info("Run %s | model rejected as not a serve: %s", artifacts.run_id, decision.get("reason"))
            decision["run_id"] = artifacts.run_id
            decision["fps"] = meta["fps"]
            decision["frame_count"] = meta["frame_count"]
            return decision

        if decision.get("status") != "found":
            logger.info("Run %s | indeterminate, trying heuristic", artifacts.run_id)
            heuristic = self.heuristic_pick_triplet(window, artifacts, meta)
            if heuristic:
                decision = heuristic

        if decision.get("status") == "found":
            before_f = decision["before_frame"]
            contact_f = decision["contact_frame"]
            after_f = decision["after_frame"]
            self.build_triplet_strip([before_f, contact_f, after_f], artifacts.output_dir / "triplet.png", artifacts)
            self.build_triplet_strip([before_f, contact_f, after_f], artifacts.output_dir / "triplet_zoom.png", artifacts, zoom=True)
            for frame in [before_f, contact_f, after_f]:
                self.ensure_annotated(frame, artifacts)
            logger.info("Run %s | contact found at frame %d (confidence: %s)", artifacts.run_id, contact_f, decision.get("confidence"))

        decision["run_id"] = artifacts.run_id
        decision["fps"] = meta["fps"]
        decision["frame_count"] = meta["frame_count"]
        decision["artifacts"] = {
            "triplet": f"runs/{artifacts.run_id}/output/triplet.png",
            "triplet_zoom": f"runs/{artifacts.run_id}/output/triplet_zoom.png",
            "contact_frame": f"runs/{artifacts.run_id}/output/frame_{decision.get('contact_frame')}.png" if decision.get("contact_frame") is not None else None,
            "contact_frame_zoom": f"runs/{artifacts.run_id}/output/frame_{decision.get('contact_frame')}_zoom.png" if decision.get("contact_frame") is not None else None,
            "contact_sheet": f"runs/{artifacts.run_id}/output/contact_sheet.png",
        }
        return decision

    # ── Frame extraction ───────────────────────────────────────────────────────

    def extract_all_frames(
        self,
        video_path: str,
        artifacts: AnalysisArtifacts,
        crop_left: float = 0.28,
        crop_right: float = 0.72,
        crop_top: float = 0.03,
        crop_bottom: float = 0.55,
    ) -> Dict[str, Any]:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError("Could not open uploaded video.")

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0:
            fps = 30.0
            logger.warning("Could not read FPS from video; defaulting to %.1f", fps)

        idx = 0
        written = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            img.save(artifacts.frames_dir / f"frame_{idx:05d}.png")
            crop = self.make_focus_crop(img, crop_left, crop_right, crop_top, crop_bottom)
            crop.save(artifacts.crops_dir / f"frame_{idx:05d}_zoom.png")
            written += 1
            idx += 1

        cap.release()

        if written == 0:
            raise RuntimeError("No frames could be read from the uploaded video.")

        self.build_contact_sheet(artifacts, written)
        logger.info("Extracted %d frames at %.2f fps", written, fps)
        return {"fps": fps, "frame_count": written}

    def make_focus_crop(
        self,
        img: Image.Image,
        crop_left: float = 0.28,
        crop_right: float = 0.72,
        crop_top: float = 0.03,
        crop_bottom: float = 0.55,
    ) -> Image.Image:
        w, h = img.size
        left = int(w * crop_left)
        right = int(w * crop_right)
        top = int(h * crop_top)
        bottom = int(h * crop_bottom)
        return img.crop((left, top, right, bottom)).resize((640, 640))

    def build_contact_sheet(self, artifacts: AnalysisArtifacts, frame_count: int, step: int = 4) -> None:
        chosen = list(range(0, frame_count, max(1, step)))[:72]
        thumbs = []
        for idx in chosen:
            path = artifacts.crops_dir / f"frame_{idx:05d}_zoom.png"
            if not path.exists():
                continue
            try:
                img = Image.open(path).convert("RGB").resize((180, 180))
            except (IOError, UnidentifiedImageError):
                logger.warning("Could not open crop for contact sheet: %s", path)
                continue
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, 84, 24), fill="white")
            draw.text((8, 5), str(idx), fill="black")
            thumbs.append(img)

        if not thumbs:
            logger.warning("No thumbnails for contact sheet; creating placeholder")
            placeholder = Image.new("RGB", (360, 180), "#cccccc")
            draw = ImageDraw.Draw(placeholder)
            draw.text((10, 80), "No thumbnails available", fill="black")
            placeholder.save(artifacts.output_dir / "contact_sheet.png")
            return

        cols = 4
        rows = math.ceil(len(thumbs) / cols)
        sheet = Image.new("RGB", (cols * 180, rows * 180), "#f4f4f4")
        for i, thumb in enumerate(thumbs):
            x = (i % cols) * 180
            y = (i // cols) * 180
            sheet.paste(thumb, (x, y))
        sheet.save(artifacts.output_dir / "contact_sheet.png")

    # ── Window selection ───────────────────────────────────────────────────────

    def pick_candidate_windows(self, search_start: int, search_end: int, frame_count: int) -> List[List[int]]:
        span = search_end - search_start
        if span <= 21:
            return [list(range(search_start, search_end + 1))]
        step = max(6, span // 12)
        groups = []
        start = search_start
        while start <= search_end:
            end = min(search_end, start + step)
            groups.append(list(range(start, end + 1)))
            start = end + 1
        return groups

    def coerce_window(self, model_output: Dict[str, Any], frame_count: int) -> Optional[Tuple[int, int]]:
        start = model_output.get("window_start")
        end = model_output.get("window_end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end < frame_count:
            return start, end
        return None

    def expand_groups(self, start: int, end: int, frame_count: int) -> List[List[int]]:
        start = max(1, start)
        end = min(frame_count - 2, end)
        groups = []
        for idx in range(start, end + 1):
            groups.append([idx - 1, idx, idx + 1])
        return groups

    # ── Image encoding ─────────────────────────────────────────────────────────

    def encode_image(self, path: Path) -> Tuple[str, str]:
        ext = path.suffix.lower().lstrip(".")
        mime = "jpeg" if ext == "jpg" else ext
        media_type = f"image/{mime}"
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return data, media_type

    def encode_image_as_jpeg(self, path: Path, max_side: int = 640) -> Tuple[str, str]:
        """Load image, downscale if needed, re-encode as JPEG to reduce payload size."""
        try:
            img = Image.open(path).convert("RGB")
        except (IOError, UnidentifiedImageError) as exc:
            raise RuntimeError(f"Cannot read image {path}: {exc}") from exc
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = base64.b64encode(buf.getvalue()).decode("utf-8")
        return data, "image/jpeg"

    # ── Phase 0: serve detection ───────────────────────────────────────────────

    def detect_serve_presence(self, frame_count: int, artifacts: AnalysisArtifacts) -> Dict[str, Any]:
        """
        Scan a sparse set of full frames to determine if a serve is present and
        where roughly it occurs.
        """
        step = max(1, frame_count // 20)  # up to ~20 thumbnails
        sample_indices = list(range(0, frame_count, step))[:20]

        content: List[Dict[str, Any]] = [
            {"type": "text", "text": SERVE_DETECTION_PROMPT},
            {"type": "text", "text": f"The video has {frame_count} frames. Here are {len(sample_indices)} evenly-spaced full frames (frame index labelled on each):"},
        ]

        for idx in sample_indices:
            path = artifacts.frames_dir / f"frame_{idx:05d}.png"
            if not path.exists():
                continue
            try:
                data, media_type = self.encode_image_as_jpeg(path, max_side=480)
            except RuntimeError as exc:
                logger.warning("Skipping frame %d in serve detection: %s", idx, exc)
                continue
            content.append({"type": "text", "text": f"Frame {idx}:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": content}],
                timeout=60,
            )
        except anthropic.APITimeoutError:
            logger.error("Serve detection API call timed out")
            # Optimistically proceed rather than block the whole analysis
            return {"serve_detected": True, "estimated_window_start": None, "estimated_window_end": None, "reason": "Detection timed out; proceeding optimistically."}
        except anthropic.APIError as exc:
            logger.error("Serve detection API error: %s", exc)
            return {"serve_detected": True, "estimated_window_start": None, "estimated_window_end": None, "reason": f"API error during detection: {exc}"}

        result = self.safe_json(response.content[0].text, context="serve_detection")
        if not result:
            logger.warning("Serve detection returned unparseable response; proceeding optimistically")
            return {"serve_detected": True, "estimated_window_start": None, "estimated_window_end": None, "reason": "Could not parse serve detection response."}
        return result

    # ── Phase 1: window selection ──────────────────────────────────────────────

    def ask_model_for_window(self, groups: List[List[int]], artifacts: AnalysisArtifacts) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [
            {"type": "text", "text": WINDOW_PROMPT},
            {"type": "text", "text": "Each window below includes a full frame (body context) followed by the zoomed upper-contact-zone crop."},
        ]

        for i, group in enumerate(groups):
            label = f"Window {i}: frames {group[0]}–{group[-1]}"
            content.append({"type": "text", "text": label})

            # Full frame for serve-mechanics context
            mid_idx = group[len(group) // 2]
            full_path = artifacts.frames_dir / f"frame_{mid_idx:05d}.png"
            if full_path.exists():
                try:
                    data, media_type = self.encode_image_as_jpeg(full_path, max_side=480)
                    content.append({"type": "text", "text": f"Full frame (frame {mid_idx}):"})
                    content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
                except RuntimeError as exc:
                    logger.warning("Skipping full frame %d in window selection: %s", mid_idx, exc)

            # Zoomed strip for contact-zone detail
            strip_path = artifacts.output_dir / f"window_{i:02d}.png"
            self.build_triplet_or_group_strip(group, strip_path, artifacts, zoom=True)
            content.append({"type": "text", "text": "Zoomed contact-zone strip:"})
            data, media_type = self.encode_image(strip_path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": content}],
                timeout=60,
            )
        except anthropic.APITimeoutError:
            logger.error("Window selection API call timed out")
            return {}
        except anthropic.APIError as exc:
            logger.error("Window selection API error: %s", exc)
            return {}

        return self.safe_json(response.content[0].text, context="window_selection")

    # ── Phase 2: contact triplet ───────────────────────────────────────────────

    def ask_model_for_triplet(self, groups: List[List[int]], artifacts: AnalysisArtifacts) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [
            {"type": "text", "text": CONTACT_PROMPT},
            {"type": "text", "text": "Review each 3-frame triplet. Each triplet is [before, candidate, after] shown as a zoomed crop strip. You must confirm BOTH serve mechanics and first contact before returning 'found'."},
        ]

        for group in groups:
            idx = group[1]
            strip_path = artifacts.output_dir / f"triplet_candidate_{idx:05d}.png"
            self.build_triplet_strip(group, strip_path, artifacts, zoom=True)
            content.append({"type": "text", "text": f"Triplet: before={group[0]}, candidate={group[1]}, after={group[2]}"})
            data, media_type = self.encode_image(strip_path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": content}],
                timeout=90,
            )
        except anthropic.APITimeoutError:
            logger.error("Triplet analysis API call timed out")
            return {"status": "indeterminate", "reason": "API call timed out during contact analysis.", "confidence": "low"}
        except anthropic.APIError as exc:
            logger.error("Triplet analysis API error: %s", exc)
            return {"status": "indeterminate", "reason": f"API error: {exc}", "confidence": "low"}

        return self.safe_json(response.content[0].text, context="triplet_analysis")

    # ── Heuristic fallback ─────────────────────────────────────────────────────

    def heuristic_pick_triplet(self, window: Tuple[int, int], artifacts: AnalysisArtifacts, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        start, end = window
        best = None
        best_score = None
        for idx in range(max(1, start), min(meta["frame_count"] - 2, end + 1)):
            prev_img = cv2.imread(str(artifacts.crops_dir / f"frame_{idx-1:05d}_zoom.png"))
            cur_img = cv2.imread(str(artifacts.crops_dir / f"frame_{idx:05d}_zoom.png"))
            next_img = cv2.imread(str(artifacts.crops_dir / f"frame_{idx+1:05d}_zoom.png"))
            if prev_img is None or cur_img is None or next_img is None:
                continue
            d1 = cv2.absdiff(cur_img, prev_img).mean()
            d2 = cv2.absdiff(next_img, cur_img).mean()
            score = abs(d1 - d2)
            if best_score is None or score > best_score:
                best_score = score
                best = idx
        if best is None:
            return None
        return {
            "status": "indeterminate",
            "before_frame": best - 1,
            "contact_frame": best,
            "after_frame": best + 1,
            "serve_confirmed": None,
            "contact_confirmed": None,
            "reason": "AI analysis was inconclusive. Heuristic best-guess shown for reference — verify visually.",
            "confidence": "low",
        }

    # ── Strip/annotation helpers ───────────────────────────────────────────────

    def build_triplet_or_group_strip(self, frames: List[int], out_path: Path, artifacts: AnalysisArtifacts, zoom: bool = False) -> None:
        images = []
        for frame in frames:
            path = artifacts.crops_dir / f"frame_{frame:05d}_zoom.png" if zoom else artifacts.frames_dir / f"frame_{frame:05d}.png"
            if not path.exists():
                logger.warning("Frame file missing for strip: %s", path)
                continue
            try:
                img = Image.open(path).convert("RGB")
            except (IOError, UnidentifiedImageError):
                logger.warning("Corrupt frame file skipped in strip: %s", path)
                continue
            img = img.resize((220, 220) if zoom else (220, 124))
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, 90, 24), fill="white")
            draw.text((8, 4), str(frame), fill="black")
            images.append(img)

        if not images:
            logger.warning("No images for strip %s; creating placeholder", out_path)
            placeholder = Image.new("RGB", (220, 220), "#cccccc")
            placeholder.save(out_path)
            return

        strip = Image.new("RGB", (220 * len(images), images[0].height), "white")
        for i, img in enumerate(images):
            strip.paste(img, (i * 220, 0))
        strip.save(out_path)

    def build_triplet_strip(self, frames: List[int], out_path: Path, artifacts: AnalysisArtifacts, zoom: bool = False) -> None:
        self.build_triplet_or_group_strip(frames, out_path, artifacts, zoom=zoom)

    def ensure_annotated(self, frame_number: int, artifacts: AnalysisArtifacts) -> None:
        full_path = artifacts.frames_dir / f"frame_{frame_number:05d}.png"
        zoom_path = artifacts.crops_dir / f"frame_{frame_number:05d}_zoom.png"
        try:
            full = Image.open(full_path).convert("RGB")
            zoom = Image.open(zoom_path).convert("RGB")
        except (IOError, UnidentifiedImageError) as exc:
            logger.error("Cannot annotate frame %d: %s", frame_number, exc)
            return
        draw_full = ImageDraw.Draw(full)
        draw_full.rectangle((0, 0, 140, 30), fill="white")
        draw_full.text((10, 7), f"Frame {frame_number}", fill="black")
        draw_zoom = ImageDraw.Draw(zoom)
        draw_zoom.rectangle((0, 0, 140, 30), fill="white")
        draw_zoom.text((10, 7), f"Frame {frame_number}", fill="black")
        full.save(artifacts.output_dir / f"frame_{frame_number}.png")
        zoom.save(artifacts.output_dir / f"frame_{frame_number}_zoom.png")

    # ── JSON parsing ───────────────────────────────────────────────────────────

    @staticmethod
    def safe_json(text: str, context: str = "unknown") -> Dict[str, Any]:
        try:
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
            return json.loads(text.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("JSON parse failure in context '%s': %s | raw: %.200s", context, exc, text)
            return {}
