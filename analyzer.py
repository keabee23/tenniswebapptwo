import base64
import json
import math
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import cv2
from PIL import Image, ImageDraw

ALLOWED_EXTENSIONS = {"mp4", "mov", "m4v", "avi", "mpeg", "mpg"}
SUPPORTED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

STRICT_PROMPT = """
You are reviewing consecutive tennis-serve video frames to identify the first frame where the ball first touches the racket ABOVE THE PLAYER'S HEAD during the actual serve strike.

Rules:
- Do not guess.
- Ignore any earlier racket-ball proximity during serve preparation.
- Do not choose the frame where the ball is merely closest to the racket.
- The correct frame must be the first frame of actual contact.
- Verify frame before = no contact.
- Verify chosen frame = first contact.
- Verify frame after = ball already compressing or departing.
- If the evidence is insufficient, return indeterminate instead of guessing.
- Contact must be above the head.

Return strict JSON with this schema (no markdown fencing, no extra text, only the JSON object):
{
  "status": "found" | "indeterminate",
  "before_frame": <int|null>,
  "contact_frame": <int|null>,
  "after_frame": <int|null>,
  "reason": "short explanation",
  "confidence": "high" | "medium" | "low"
}
""".strip()


@dataclass
class AnalysisArtifacts:
    run_id: str
    run_dir: Path
    frames_dir: Path
    crops_dir: Path
    output_dir: Path


class ContactAnalyzer:
    def __init__(self, runs_root: str, model: str = "claude-sonnet-4-20250514"):
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.model = model
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None

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

    def analyze(self, video_path: str) -> Dict[str, Any]:
        artifacts = self.prepare_run()
        meta = self.extract_all_frames(video_path, artifacts)
        frame_count = meta["frame_count"]
        if frame_count < 3:
            return {
                "status": "indeterminate",
                "reason": "Video has fewer than 3 readable frames.",
                "run_id": artifacts.run_id,
            }

        first_pass = self.pick_candidate_windows(frame_count)
        shortlist = self.ask_model_for_window(first_pass, artifacts, scope="window")
        window = self.coerce_window(shortlist, frame_count) or self.fallback_window(frame_count)

        refined_groups = self.expand_groups(*window, frame_count=frame_count)
        decision = self.ask_model_for_triplet(refined_groups, artifacts)

        if decision.get("status") != "found":
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

    def extract_all_frames(self, video_path: str, artifacts: AnalysisArtifacts) -> Dict[str, Any]:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError("Could not open uploaded video.")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        idx = 0
        written = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            img.save(artifacts.frames_dir / f"frame_{idx:05d}.png")
            crop = self.make_focus_crop(img)
            crop.save(artifacts.crops_dir / f"frame_{idx:05d}_zoom.png")
            written += 1
            idx += 1

        cap.release()
        self.build_contact_sheet(artifacts, written)
        return {"fps": fps, "frame_count": written or total}

    def make_focus_crop(self, img: Image.Image) -> Image.Image:
        w, h = img.size
        left = int(w * 0.28)
        right = int(w * 0.72)
        top = int(h * 0.03)
        bottom = int(h * 0.55)
        return img.crop((left, top, right, bottom)).resize((640, 640))

    def build_contact_sheet(self, artifacts: AnalysisArtifacts, frame_count: int, step: int = 4) -> None:
        chosen = list(range(0, frame_count, max(1, step)))[:72]
        thumbs = []
        for idx in chosen:
            path = artifacts.crops_dir / f"frame_{idx:05d}_zoom.png"
            if not path.exists():
                continue
            img = Image.open(path).convert("RGB").resize((180, 180))
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, 84, 24), fill="white")
            draw.text((8, 5), str(idx), fill="black")
            thumbs.append(img)

        if not thumbs:
            return

        cols = 4
        rows = math.ceil(len(thumbs) / cols)
        sheet = Image.new("RGB", (cols * 180, rows * 180), "#f4f4f4")
        for i, thumb in enumerate(thumbs):
            x = (i % cols) * 180
            y = (i // cols) * 180
            sheet.paste(thumb, (x, y))
        sheet.save(artifacts.output_dir / "contact_sheet.png")

    def pick_candidate_windows(self, frame_count: int) -> List[List[int]]:
        if frame_count <= 21:
            return [list(range(frame_count))]
        step = max(6, frame_count // 12)
        groups = []
        start = 0
        while start < frame_count:
            end = min(frame_count - 1, start + step)
            groups.append(list(range(start, end + 1)))
            start = end + 1
        return groups

    def coerce_window(self, model_output: Dict[str, Any], frame_count: int) -> Optional[Tuple[int, int]]:
        start = model_output.get("window_start")
        end = model_output.get("window_end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end < frame_count:
            return start, end
        return None

    def fallback_window(self, frame_count: int) -> Tuple[int, int]:
        center = max(1, frame_count // 2)
        return max(0, center - 6), min(frame_count - 1, center + 6)

    def expand_groups(self, start: int, end: int, frame_count: int) -> List[List[int]]:
        start = max(1, start)
        end = min(frame_count - 2, end)
        groups = []
        for idx in range(start, end + 1):
            groups.append([idx - 1, idx, idx + 1])
        return groups

    def encode_image(self, path: Path) -> Tuple[str, str]:
        ext = path.suffix.lower().lstrip(".")
        mime = "jpeg" if ext == "jpg" else ext
        media_type = f"image/{mime}"
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return data, media_type

    def ask_model_for_window(self, groups: List[List[int]], artifacts: AnalysisArtifacts, scope: str) -> Dict[str, Any]:
        if not self.client:
            return {}

        content: List[Dict[str, Any]] = [
            {"type": "text", "text": STRICT_PROMPT},
            {"type": "text", "text": "First, choose the most likely frame window containing the above-head serve contact. Return JSON: {\"window_start\": int, \"window_end\": int, \"reason\": string}."},
        ]
        for i, group in enumerate(groups):
            strip_path = artifacts.output_dir / f"window_{i:02d}.png"
            self.build_triplet_or_group_strip(group, strip_path, artifacts, zoom=True)
            label = f"Window {i}: frames {group[0]}-{group[-1]}"
            content.append({"type": "text", "text": label})
            data, media_type = self.encode_image(strip_path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})

        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
        return self.safe_json(response.content[0].text)

    def ask_model_for_triplet(self, groups: List[List[int]], artifacts: AnalysisArtifacts) -> Dict[str, Any]:
        if not self.client:
            return {"status": "indeterminate", "reason": "ANTHROPIC_API_KEY is not set.", "confidence": "low"}

        content: List[Dict[str, Any]] = [
            {"type": "text", "text": STRICT_PROMPT},
            {"type": "text", "text": "Review each 3-frame triplet. Each triplet is [before, candidate, after]. Choose only one triplet if it satisfies the rules. Otherwise return indeterminate."},
        ]
        for group in groups:
            idx = group[1]
            strip_path = artifacts.output_dir / f"triplet_candidate_{idx:05d}.png"
            self.build_triplet_strip(group, strip_path, artifacts, zoom=True)
            content.append({"type": "text", "text": f"Triplet centered on frame {idx}: before={group[0]}, contact?={group[1]}, after={group[2]}"})
            data, media_type = self.encode_image(strip_path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})

        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
        return self.safe_json(response.content[0].text)

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
            "status": "found",
            "before_frame": best - 1,
            "contact_frame": best,
            "after_frame": best + 1,
            "reason": "Fallback heuristic selected the strongest transition in the above-head crop. Verify visually.",
            "confidence": "low",
        }

    def build_triplet_or_group_strip(self, frames: List[int], out_path: Path, artifacts: AnalysisArtifacts, zoom: bool = False) -> None:
        images = []
        for frame in frames:
            path = artifacts.crops_dir / f"frame_{frame:05d}_zoom.png" if zoom else artifacts.frames_dir / f"frame_{frame:05d}.png"
            img = Image.open(path).convert("RGB")
            if zoom:
                img = img.resize((220, 220))
            else:
                img = img.resize((220, 124))
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, 90, 24), fill="white")
            draw.text((8, 4), str(frame), fill="black")
            images.append(img)
        strip = Image.new("RGB", (220 * len(images), images[0].height), "white")
        for i, img in enumerate(images):
            strip.paste(img, (i * 220, 0))
        strip.save(out_path)

    def build_triplet_strip(self, frames: List[int], out_path: Path, artifacts: AnalysisArtifacts, zoom: bool = False) -> None:
        self.build_triplet_or_group_strip(frames, out_path, artifacts, zoom=zoom)

    def ensure_annotated(self, frame_number: int, artifacts: AnalysisArtifacts) -> None:
        full = Image.open(artifacts.frames_dir / f"frame_{frame_number:05d}.png").convert("RGB")
        zoom = Image.open(artifacts.crops_dir / f"frame_{frame_number:05d}_zoom.png").convert("RGB")
        draw_full = ImageDraw.Draw(full)
        draw_full.rectangle((0, 0, 140, 30), fill="white")
        draw_full.text((10, 7), f"Frame {frame_number}", fill="black")
        draw_zoom = ImageDraw.Draw(zoom)
        draw_zoom.rectangle((0, 0, 140, 30), fill="white")
        draw_zoom.text((10, 7), f"Frame {frame_number}", fill="black")
        full.save(artifacts.output_dir / f"frame_{frame_number}.png")
        zoom.save(artifacts.output_dir / f"frame_{frame_number}_zoom.png")

    @staticmethod
    def safe_json(text: str) -> Dict[str, Any]:
        try:
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
            return json.loads(text.strip())
        except Exception:
            return {}
