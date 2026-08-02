# Plan: Photo → Final Video Website (Phases 2 + 3)

## Goal

A website where the user uploads a photo in `index.html`, positions the face, clicks
**Generate Video**, watches a live progress bar, and previews/downloads the finished
`final.mp4` — the same result the Python pipeline produces at the end of phase 3.
No After Effects, no manual export step: the face.png is exported in the browser
(canvas + clip-path) and sent straight to the server.

## Flow

```
templates/index.html
  ├─ Upload photo → position → click "Generate Video"
  │    └─ JS: getFacePngBlob() → canvas.toBlob() → face.png (112×168, clipped)
  │         └─ POST /generate (multipart)
  │              ↓  202 {"job_id"} — render runs in a background thread
  │         browser polls GET /api/progress/<job_id> every 500ms → progress bar
  │              ↓  status "done"
  │         GET /api/result/<job_id> → final.mp4 → shown in <video>, Download button
  └─ Preview + download final.mp4
```

## Folder & File Structure

```
website/
├── app.py                     # Flask: /, /generate, /api/progress/<id>, /api/result/<id>
│                              # job registry (thread-safe, capped), render thread, console logging
├── config.py                  # paths, video spec, render knobs, progress phase weights
├── requirements.txt           # flask, opencv-python, numpy
├── .gitignore                 # uploads/, tmp/, __pycache__
├── README.md                  # setup + run + verify instructions
│
├── assets/                    # read-only pipeline inputs
│   ├── tracking/track_result.json   # animation data (fixed)
│   ├── background/BG.mp4            # background video with audio
│   └── foreground/fg_preview.mp4    # rotoscoped foreground
│
├── services/
│   ├── overlay.py             # phase 2: face-over-background renderer (+progress callback)
│   ├── composite.py           # phase 3: brightness-matte composite (+progress callback)
│   └── media.py               # ffmpeg remux (parses -progress output) + ffprobe helpers
│
├── static/
│   └── favicon.svg            # page icon
├── templates/
│   └── index.html             # UI: upload, position, Generate Video, progress bar, video player
│                              # dark claymorphism theme (pink #EC4899 / blue #2563EB on #0f172a)
│                              # Fredoka + Nunito fonts
│
├── uploads/                   # per-job user face.png (temp; wiped at startup, cleaned after render)
└── tmp/                       # per-job intermediate videos (temp; wiped at startup, cleaned after render)
```

## API

| Endpoint | Behavior |
|---|---|
| `POST /generate` | Saves face.png, registers job (before thread start), spawns daemon thread → `202 {job_id}` |
| `GET /api/progress/<job_id>` | `{status, phase, frame, total, percent, error}` — 404 if unknown |
| `GET /api/result/<job_id>` | 409 while rendering / 500 with message on error / 200 video bytes when done; file + job entry deleted after serving |

## Render job lifecycle

- Thread renders phase 2 → phase 3 (each with per-frame progress callbacks, exception-safe),
  then deletes `face.png` + `bg_preview`; the final video is kept until `/api/result` serves it.
- Errors: thread catches everything → `status:"error"` + message + full cleanup (never hangs the client).
- Registry capped at 20 jobs (oldest finished evicted, files deleted); server restart wipes
  `uploads/` + `tmp/` leftovers.

## Progress model

| Phase | Percent range | Source |
|---|---|---|
| overlay (face over background) | 0 → 14% | per-frame callback, `CAP_PROP_FRAME_COUNT` total |
| finalize_bg (phase 2 audio remux) | holds 14% | remux start signal + ffmpeg `-progress` |
| composite (fg over bg) | 14 → 90% | per-frame callback |
| remux (final audio mux) | 90 → 100% | ffmpeg `out_time_us` / duration |
| done | 100% | client sets on `status:"done"` before showing video |

UI: pink gradient fill bar with glow, pulse during remux, phase labels
("Placing face on background… 45%", "Finalizing…"), 500ms polling, CSS width transition,
`prefers-reduced-motion` respected.

## UI (index.html)

- Header "Birthday Clip Studio" + logo; dark claymorphism cards, chunky sliders/buttons.
- Export PNG button removed; only Generate Video (+ Download final.mp4 after render).
- Result plays in an in-page `<video>`; browser console logs each step; server logs
  per-job timings (`[jobid] Phase 2 done in 1.5s` …).

## Notes / gotchas (baked in)

- `track_result.json` has a UTF-8 BOM → read with `utf-8-sig`.
- Rotation sign flip (`-rotation`) preserved from the original scripts.
- Frames with opacity ≤ 0 are skipped.
- Unique temp filenames per job (concurrent renders cannot collide).
- ffmpeg emits `-progress` reports at most every 0.5s — remux start is signaled explicitly
  so the phase is always visible.
- Run locally: `pip install -r requirements.txt` && `python app.py` → http://127.0.0.1:5000.
  Verify renders with ffprobe (expect 568×320, 24 fps, aac).
