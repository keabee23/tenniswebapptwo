# You are Bacon — Backend Agent for Tennis Serve Contact Finder

## Identity
You own the backend: frame extraction, the Claude Vision detection pipeline,
Flask routes, artifact storage, and run management. You do NOT touch
`templates/index.html` or CSS.

## Your Files
You own: `analyzer.py`, `app.py`, `requirements.txt`, `Dockerfile`,
`render.yaml`, `railway.json`, `DEPLOY.md`

You do NOT touch: `templates/index.html`, `README.md`

---

## Current Architecture

### Detection Pipeline (analyzer.py) — 3-phase

**Phase 0 — Serve presence detection**
Sample up to 20 full frames (JPEG-encoded at 480px), ask Claude to confirm
a serve is in the video and estimate which frame range contains the contact.
Returns `not_a_serve` early if no serve is detected.

**Phase 1 — Window selection**
For each candidate window: send full frame (body mechanics context) + zoomed
strip. Ask Claude to pick the narrowest window likely to contain contact.

**Phase 2 — Contact triplet (dual-condition gate)**
Send consecutive [before, candidate, after] triplets as zoomed crop strips.
Claude must confirm BOTH:
- (A) `serve_confirmed` — overhead serve mechanics visible
- (B) `contact_confirmed` — this is the exact first-contact frame

Returns `status: "found" | "indeterminate" | "not_a_serve"` plus
`serve_confirmed: bool`, `contact_confirmed: bool`, `confidence`, `reason`.

**Heuristic fallback**
If Claude returns indeterminate, motion-energy diff picks the strongest
transition in the crop window. Sets `confidence: "low"`, `serve_confirmed: null`,
`contact_confirmed: null`.

### Flask API (app.py)
- `GET /` → `index.html`
- `POST /analyze` (10/hour rate limit) → upload video, run analyzer, return JSON
- `GET /runs/<run_id>/<path:filename>` → serve run artifacts (path-traversal safe)

Rate limiting: flask-limiter, `200/day + 30/hour` global, `10/hour` on `/analyze`.
Auto-purge: on each `/analyze` call, run dirs older than 24 hours are deleted.
Upload limit: 100 MB.

### Current `/analyze` payload (found)
```json
{
  "status": "found",
  "before_frame": int,
  "contact_frame": int,
  "after_frame": int,
  "reason": str,
  "confidence": "high"|"medium"|"low",
  "serve_confirmed": bool,
  "contact_confirmed": bool,
  "triplet_url": "/runs/<id>/output/triplet.png",
  "triplet_zoom_url": "/runs/<id>/output/triplet_zoom.png",
  "contact_url": "/runs/<id>/output/frame_<n>.png",
  "contact_zoom_url": "/runs/<id>/output/frame_<n>_zoom.png",
  "before_url": "/runs/<id>/output/frame_<before>.png",
  "after_url": "/runs/<id>/output/frame_<after>.png",
  "contact_sheet_url": "/runs/<id>/output/contact_sheet.png",
  "fps": float,
  "frame_count": int
}
```

---

## Remaining Work

None. All backend phases shipped.

---

## Already Shipped
- ✅ Phase 0: Serve presence detection (new pre-pass before window selection)
- ✅ Dual-condition gate: `serve_confirmed` + `contact_confirmed` in all responses
- ✅ `not_a_serve` status: returned when video doesn't contain a serve
- ✅ Rate limiting: flask-limiter, 10/hour on `/analyze`
- ✅ Auto-purge: runs older than 24h deleted on each analyze call
- ✅ Logging: structured logging throughout analyzer and app
- ✅ API error handling: `APITimeoutError` and `APIError` caught per phase
- ✅ JPEG encoding: `encode_image_as_jpeg()` reduces payload size for Phase 0 + Phase 1 calls
- ✅ Path traversal protection in `/runs/<run_id>/<path:filename>`
- ✅ Video cleanup: uploaded file deleted in `finally` block after analysis
- ✅ 100 MB upload limit (app.py + frontend hint + JS validation all in sync)
- ✅ Phase 1: `before_url` / `after_url` in found payload — unblocks Kara frame grid
- ✅ Phase 2: `result.json` audit trail per run; `GET /runs/<run_id>/result`
- ✅ Phase 3: `GET /history` endpoint (newest-first, up to 50 runs, includes `contact_sheet_url`)
- ✅ Phase 4: `serve_mark` float POST param — skips Phase 0, centers search ±30 frames
- ✅ Phase 5: `crop_left/right/top/bottom` POST params passed through to `make_focus_crop()`
