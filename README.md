# Photo → Final Video Website

Turns a photo into the finished birthday-clip video: upload a face photo, position it
against the guide lines, click **Generate Video**, and preview/`download final.mp4` —
the same output the Python pipeline (phases 2 + 3) produces, entirely from the browser.

## How it works

1. `templates/index.html` — upload + position the face, then export a 112×168 clipped
   `face.png` in the browser (canvas) and POST it to the server.
2. `services/overlay.py` — phase 2: animates the face over `BG.mp4` using the tracking
   data in `assets/tracking/track_result.json` → `bg_preview.mp4`.
3. `services/composite.py` — phase 3: composites the rotoscoped foreground
   (`assets/foreground/fg_preview.mp4`) over it with a brightness matte → `final.mp4`.
4. `services/media.py` — ffmpeg remux (H.264 + AAC audio) shared by both phases.

## Setup

```bash
cd website
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 in a browser.

Requires `ffmpeg`/`ffprobe` on PATH (OpenCV cannot write AAC audio).

## Layout

```
app.py            Flask routes (/ and /generate)
config.py         paths, video spec, render tuning knobs
services/         render pipeline (overlay, composite, media)
assets/           read-only inputs (tracking JSON, BG.mp4, fg_preview.mp4)
templates/        index.html (face guide UI)
static/           page assets (css/js/images)
uploads/          user face.png (temp, cleaned after each render)
tmp/              intermediate videos (temp, cleaned after each render)
```

## Verifying a render

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -of csv=p=0 <video>
# expect: 568,320,24/1
```

## Tuning the phase 3 key

Edit `config.py`: `BLACK_CUTOFF` (raise if dark areas turn see-through),
`FEATHER_RANGE` (raise if edges are hard), `ERODE_ITERATIONS` (raise if a dark halo
persists).

## Deployment (Render)

- Python version is pinned via `.python-version` (3.14.3, Render's default).
- Start command (`Procfile`): `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app`
- **Keep exactly one worker.** The job registry lives in process memory; multiple
  workers would each get their own registry and progress polls would randomly
  return "Unknown job." Do not scale beyond one instance without moving job
  state to a shared datastore (Redis).
- `GET /health` returns `{"status": "ok"}` — use it as Render's health-check path.
- Only **one render runs at a time**: while a job is active, further `POST /generate`
  calls return HTTP 429 with a "please wait" message. OpenCV + ffmpeg would
  overwhelm a small instance if several users generated videos together.
- Render keeps the gunicorn `--timeout 120` from recycling the worker mid-render:
  the browser polls `/api/progress` every 500 ms, and the `/health` checks reset
  the same timer, so the worker stays alive even if the user closes the tab.
- `opencv-python-headless` replaces `opencv-python` (same cv2, no GUI libraries);
  Render images ship ffmpeg, which `services/media.py` calls directly.
