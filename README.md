# Tennis Serve Contact Finder

A small Flask web app that lets a user upload a tennis serve video and tries to identify the **first frame where the ball first touches the racket above the player's head**.

## How it works

1. Upload a video.
2. The backend extracts every frame with OpenCV.
3. It builds a zoomed crop aimed at the above-head contact area.
4. It asks an OpenAI vision model to:
   - choose the most likely window containing the real serve strike,
   - compare consecutive triplets,
   - return one strict answer only if it can verify:
     - frame before = no contact,
     - chosen frame = first contact,
     - frame after = ball already compressing or departing.
5. The app returns:
   - before / contact / after strip,
   - zoomed strip,
   - chosen frame,
   - contact sheet,
   - explanation.

## Important limitation

This app is only as good as the footage. It should say **indeterminate** instead of guessing when:
- frame rate is too low,
- motion blur is too heavy,
- the ball is hidden,
- the serve contact is outside the crop.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```bash
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5
```

Run:

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Notes

- Supported upload types: `mp4`, `mov`, `m4v`, `avi`, `mpg`, `mpeg`
- Max upload size is set to 512 MB.
- Cropping is currently fixed to a center / upper-body region. You can improve results by making the crop configurable per camera angle.

## Suggested next improvements

- Let the user draw a custom crop box before analysis.
- Add a frame scrubber for manual verification.
- Save a CSV or JSON audit trail for each run.
- Run a two-pass search with finer frame windows around the serve strike.
- Add optional pose tracking to locate the head and keep the crop centered above it.


## Deploy

This repo now includes Docker and cloud deployment files for Render and Railway.

Required environment variables:
- `OPENAI_API_KEY`
- `OPENAI_MODEL` (optional, defaults to `gpt-5`)
