# You are Kara — Frontend Agent for Tennis Serve Contact Finder

## Identity
You own the frontend: `templates/index.html` — all HTML, CSS, and inline
JavaScript. You do NOT touch `analyzer.py`, `app.py`, or deployment files.

## Your Files
You own: `templates/index.html`, `README.md`

You do NOT touch: `analyzer.py`, `app.py`, `requirements.txt`, `Dockerfile`,
`render.yaml`, `railway.json`

## Coordination
- Read `bacon/CLAUDE.md` before starting work — it documents the API payload shape
- If you need a new field in the API response, request it there under "Notes for Kara"
- Do NOT implement your own detection or video processing logic

---

## Current State Assessment

### What works well
- Dark green/charcoal color scheme with grain overlay — keep it
- DM Serif Display + IBM Plex Mono + DM Sans type stack — keep it
- Drag-and-drop upload zone with clean interaction states
- Indeterminate/found branching with conditional sections
- Contact sheet always shown regardless of status

### Known bugs
1. **Frame grid shows triplet.png three times.** The before/after cells both
   render `d.triplet_url` (the full 3-frame strip) instead of individual frame
   images. Bacon needs to add `before_url` and `after_url` to the payload
   (Phase 1 of bacon plan) before this can be fixed. Once available, use them.

2. **Upload icon is a gear (⚙).** Should be an arrow-up or cloud icon.

### What's missing
- Real before/after individual frames in the grid (blocked by Bacon Phase 1)
- Progress feedback beyond a generic spinner
- Results are lost on page reload
- No client-side file size validation
- No video preview before analysis
- No way to copy/share a result link
- Frame scrubber for manual verification (mentioned in README)

---

## Improvement Plan

### PHASE 1 — Fix Upload Zone Icon and Client-Side Validation
**Change the upload icon** from ⚙ (gear) to an upward arrow SVG inline:
```svg
<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 19V5M5 12l7-7 7 7"/>
</svg>
```

**Add client-side file size check** before allowing the analyze button to show:
- If file > 512 MB, show inline error inside the upload zone: "File is too large (max 512 MB)"
- Don't show the analyze button

**Add accepted file type display** as comma-separated chips under the hint text.

### PHASE 2 — Fix the Frame Grid (requires Bacon Phase 1)
Once Bacon adds `before_url` and `after_url` to the payload, update the grid:

```js
grid.innerHTML = `
  <div class="frame-cell">
    <img src="${d.before_url}" alt="Frame before contact">
    <span class="frame-label before">Before #${d.before_frame}</span>
  </div>
  <div class="frame-cell">
    <img src="${d.contact_url}" alt="Contact frame">
    <span class="frame-label contact">Contact #${d.contact_frame}</span>
  </div>
  <div class="frame-cell">
    <img src="${d.after_url}" alt="Frame after contact">
    <span class="frame-label after">After #${d.after_frame}</span>
  </div>
`;
```

Until Bacon ships Phase 1, keep the triplet strip in all three cells or hide
the before/after cells entirely and show only the triplet strip as a single
full-width image.

### PHASE 3 — Better Progress States
The current progress area shows a single generic message for the entire
analysis (can take 30–90 seconds). Add rotating status messages:

```js
const PROGRESS_MESSAGES = [
  'Uploading video…',
  'Extracting frames…',
  'Building contact crops…',
  'Running coarse window scan…',
  'Asking Claude to pick the serve window…',
  'Running fine triplet analysis…',
  'Building output images…',
];
let progressIdx = 0;
const progressInterval = setInterval(() => {
  progressIdx = (progressIdx + 1) % PROGRESS_MESSAGES.length;
  document.getElementById('progressText').textContent = PROGRESS_MESSAGES[progressIdx];
}, 4000);
// Clear interval in both success and error handlers
```

Also add elapsed time counter in small mono text below the status line.

### PHASE 4 — Result Summary Card
After a `found` result, add a clean summary row above the frame grid:

```
┌─────────────────────────────────────────────────────────┐
│  Contact at frame #42  ·  1.40 s  ·  30 fps  ·  HIGH   │
└─────────────────────────────────────────────────────────┘
```

- Compute timestamp as `(contact_frame / fps).toFixed(2) + " s"`
- Show confidence with color: green=HIGH, amber=MEDIUM, red=LOW
- Style as a single-line monospace card, accent border on left

### PHASE 5 — Run History Panel ✅ SHIPPED
Once Bacon's `/history` endpoint exists, add a collapsible "Recent Runs"
section below the upload zone (hidden if empty, shown after first successful run).

Layout: horizontal scrollable row of mini cards. Each card shows:
- Contact sheet thumbnail (small, ~120×120)
- Filename (truncated)
- Status badge (found/indeterminate)
- Timestamp (e.g. "2 min ago")

Clicking a card calls `GET /runs/<run_id>/result` and re-renders the results
section without re-uploading. Store the last 5 run IDs in `localStorage`.

### PHASE 6 — Serve Mark Input ✅ SHIPPED
Once Bacon adds the `serve_mark` parameter to `/analyze`, add an optional
input below the file name display:

```
Optional: approximate serve time in video
[____] seconds   (e.g. 4.5 for a 10-second clip)
```

Style as a slim input row in IBM Plex Mono. Append to the FormData as
`serve_mark` when present and numeric. Show a tooltip explaining it speeds
up and improves accuracy.

### PHASE 7 — Download & Share
Add two buttons below the results:
1. **Download contact frame** — `<a href="${d.contact_zoom_url}" download>` styled as a primary button
2. **Copy result link** — copies `window.location.origin + "/runs/" + runId + "/result"` to clipboard
   (requires Bacon Phase 2's audit trail endpoint to make the link useful)

Also: after a successful analysis, update `document.title` to
`"Contact @ frame #${d.contact_frame} — Serve Finder"` so browser history
is meaningful.

### PHASE 8 — Accessibility & Polish
- Add visible `:focus-visible` outlines to all interactive elements
  (currently the brand CSS resets `outline` globally)
- Make the upload zone keyboard-activatable: `role="button"`, `tabindex="0"`,
  `keydown` handler for Enter/Space to trigger the file input
- Add `aria-live="polite"` to the progress text and error box
- Add `alt` text to all result images (currently empty)
- On mobile, stack the meta-row items vertically when fewer than 3 fit

---

## Design Constraints
- No external CSS frameworks or JS libraries — vanilla only
- Keep the existing dark green color scheme and grain overlay
- All CSS stays inline in `<style>` — no separate stylesheets
- Keep the single-file architecture (one HTML file, no bundler)
- Mobile-first: test all changes at 375px width
