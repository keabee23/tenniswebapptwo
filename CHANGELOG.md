# Changelog — Tennis Serve Contact Finder

All notable changes to this project are documented here.

---

## [Unreleased]

All planned phases complete.

---

## [Current] — Major backend rewrite + frontend improvements

### Backend — Analyzer (analyzer.py)
- **New Phase 0** — Serve presence detection: sample up to 20 full frames, ask Claude if a serve is present and where. Returns `not_a_serve` early if no serve detected. Also returns `estimated_window_start/end` hint to narrow Phase 1 search.
- **Dual-condition contact gate** — Claude now confirms BOTH `serve_confirmed` (overhead serve mechanics) AND `contact_confirmed` (exact first-contact frame) before returning `found`. If serve is confirmed but contact is unclear, returns `indeterminate` with explanation. If contact is seen but it's not a serve, returns `not_a_serve`.
- **New `not_a_serve` status** — distinct from `indeterminate`. Means the video doesn't show a serve at all (e.g. groundstroke, volley, or no tennis content).
- **`serve_confirmed` / `contact_confirmed` fields** — returned in all `found` and `indeterminate` responses (null when from heuristic fallback).
- **`encode_image_as_jpeg()`** — new helper that downscales and re-encodes images as JPEG before sending to Claude; reduces API payload size significantly.
- **API error handling** — `anthropic.APITimeoutError` and `anthropic.APIError` caught per phase with graceful fallback (serve detection times out → proceed optimistically; contact analysis times out → return indeterminate).
- **Logging** — structured logging throughout with `run_id` context on every message.
- **Window prompt updated** — sends both full frame (body mechanics) and zoomed crop per window; improved prompt clarity.
- **Heuristic fallback** — now sets status to `indeterminate` (not `found`) and includes `serve_confirmed: null`, `contact_confirmed: null`.
- **Robust frame handling** — missing/corrupt frame files skipped with warning rather than crashing; placeholder contact sheet if no thumbnails available.

### Backend — Flask App (app.py)
- **Rate limiting** — flask-limiter: `200/day + 30/hour` global, `10/hour` on `/analyze` endpoint.
- **Auto-purge** — on each `/analyze` call, run directories older than 24 hours are deleted automatically.
- **`not_a_serve` response** — separate JSON branch for not-a-serve status.
- **`serve_confirmed`/`contact_confirmed`** — included in `found` payload.
- **Uploaded video cleanup** — video file deleted in `finally` block after analysis completes or fails.
- **Path traversal protection** — `serve_run_file` rejects any path component that is `..`, `.`, or empty.
- **Logging** — upload size, run results, purge events all logged.
- **100 MB upload limit** — reduced from 512 MB.
- **Hard API key requirement** — `ContactAnalyzer` raises `RuntimeError` at startup if `ANTHROPIC_API_KEY` is not set.

### Backend — Dependencies (requirements.txt)
- Added `flask-limiter==3.9.0`

### Frontend (templates/index.html)
- **Upload icon** — changed from gear (⚙) to SVG upload arrow
- **File size validation** — client-side check blocks files over 100 MB with inline error; hint text updated to "up to 100 MB"
- **MAX_SIZE** — synchronized: JS (100 MB), hint text (100 MB), app.py (100 MB)
- **Frame grid** — uses individual `before_url`/`after_url` if available (pending Bacon Phase 1); falls back to showing triplet strip as single full-width image
- **Progress messages** — 7 rotating status messages every 4 seconds ("Uploading video…" through "Building output images…") with live elapsed-time counter
- **Summary card** — appears on `found` results: contact frame number, timestamp in seconds, FPS, confidence (color-coded green/amber/red)
- **Confirmation flags** — `serve_confirmed` and `contact_confirmed` shown as ✓/✗/? badges below the reason text when present
- **`not_a_serve` badge** — red styling for the new status
- **Download button** — links to zoomed contact frame with `download` attribute
- **Copy result link** — copies contact frame URL to clipboard with "Copied!" feedback
- **Browser title** — updates to `"Contact @ frame #42 — Serve Finder"`, `"Not a Serve — Serve Finder"`, or `"Indeterminate — Serve Finder"` based on result
- **Accessibility** — `:focus-visible` outlines on all interactive elements; `role="button"` + `tabindex="0"` + Enter/Space keyboard activation on upload zone; `aria-live="polite"` on progress text; `aria-live="assertive"` on error box; descriptive `alt` text on all result images; mobile meta-row stacks vertically

---

## [Previous] — Initial working version

### Backend
- Flask app with `/analyze` POST and `/runs/<id>/<path>` static file serving
- Two-pass Claude Vision detection: coarse window selection → fine triplet analysis
- Motion-energy heuristic fallback
- Artifacts: triplet strip, zoomed triplet, annotated contact frame, contact sheet
- Configurable model via `ANTHROPIC_MODEL` env var
- Docker, Render, Railway deployment configs

### Frontend
- Single-page dark green UI with grain overlay
- Drag-and-drop upload zone
- Results: status badge, confidence badge, reason, meta stats, frame grid, contact sheet
- Known issues (now fixed): frame grid showed triplet strip 3× instead of individual frames; upload icon was a gear; no progress feedback; no file size validation
