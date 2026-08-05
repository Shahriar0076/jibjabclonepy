"""Flask app: serves the face-guide page and renders the final video.

Run locally with:
    pip install -r requirements.txt
    python app.py

Render flow: POST /generate starts a background thread and returns a
job_id immediately; the browser polls /api/progress/<job_id> for live
progress and fetches the finished video from /api/result/<job_id>.
"""

import logging
import os
import threading
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

import config
from services import RenderCancelled, composite, overlay

# Console logging with timestamps (visible in the terminal running app.py).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("website")

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB uploads

# In-memory job registry: job_id -> {status, phase, frame, total, percent,
# error, final_video, cancel_requested}. Jobs survive only as long as the
# process.
jobs = {}
jobs_lock = threading.Lock()

MAX_JOBS = 20

# Only one CPU-heavy render may run at a time. OpenCV + ffmpeg can
# overwhelm a small instance, so busy /generate calls get 429.
MAX_ACTIVE_RENDERS = 1
RENDER_BUSY_MESSAGE = (
    "A video is already being generated. "
    "Please wait for it to finish and try again."
)


@app.errorhandler(RequestEntityTooLarge)
def file_too_large(error):
    log.warning("Upload rejected: exceeds 20 MB limit")
    return jsonify({"error": "File too large (max 20 MB)."}), 413


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    """Lightweight liveness probe for Render (no heavy work)."""
    return jsonify({"status": "ok"})


@app.route("/assets/<path:filename>")
def assets(filename):
    """Serve pipeline assets (e.g. the landing-page background video)."""
    return send_from_directory(config.ASSETS_DIR, filename)


# ----------------------------
# Job helpers
# ----------------------------
def _update_job(job_id, **fields):
    """Update a job record; maintain the ETA progress history."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return

        # A new phase has its own percent-vs-time slope: stale points
        # from the previous phase would skew the ETA fit.
        if "phase" in fields and fields["phase"] != job.get("phase"):
            job["eta_history"] = []

        if "percent" in fields and fields["percent"] != job.get("percent"):
            history = job.setdefault("eta_history", [])
            now = time.time()
            history.append((now, fields["percent"]))

            # Prune: keep points within the window, then cap the length
            # (drop the OLDEST extras so the fit always uses recent data).
            cutoff = now - ETA_HISTORY_WINDOW
            first = 0
            while first < len(history) and history[first][0] < cutoff:
                first += 1
            del history[:first]
            del history[:max(0, len(history) - ETA_HISTORY_MAX)]

        job.update(fields)


def _is_cancelled(job_id):
    """True when the user has requested this job be cancelled."""
    with jobs_lock:
        job = jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


def _estimate_eta(history, percent):
    """Seconds until the render hits 100%, from a least-squares fit of
    recent (timestamp, percent) points. None when there is not enough
    data or the rate is not measurable (e.g. percent is frozen).
    """
    if percent >= 100:
        return 0

    now = time.time()
    pts = [(t, p) for t, p in history if now - t <= ETA_HISTORY_WINDOW]

    if len(pts) < 3:
        return None

    n = len(pts)

    # Center timestamps around their mean: raw epoch values (~1.7e9) would
    # cancel catastrophically in the LS sums (float64 keeps ~16 digits).
    t0 = sum(t for t, _ in pts) / n
    sxx = sum((t - t0) * (t - t0) for t, _ in pts)
    sxy = sum((t - t0) * p for t, p in pts)

    if sxx <= 0:
        return None

    slope = sxy / sxx  # percent gained per second
    if slope <= 0:
        return None

    eta = (100.0 - percent) / slope
    return round(min(max(eta, 0.0), 3600.0))


ETA_HISTORY_WINDOW = 30.0  # seconds of progress history used for the ETA fit
ETA_HISTORY_MAX = 200      # hard cap on stored (time, percent) points


def _evict_old_jobs_locked():
    """Keep the registry bounded; delete leftover files of evicted jobs."""
    for key in list(jobs):
        if len(jobs) <= MAX_JOBS:
            break
        job = jobs[key]
        if job["status"] in ("done", "error", "cancelled"):
            final_video = job.get("final_video")
            if final_video:
                try:
                    if os.path.exists(final_video):
                        os.remove(final_video)
                except OSError:
                    pass
            jobs.pop(key)


def _make_progress_callback(job_id, phase, lo, hi, remux_phase="remux",
                            remux_target=None):
    """Map service progress calls onto the job's percent range.

    Frame calls map linearly from `lo` to `hi`. The audio-remux calls
    advance from `hi` toward `remux_target` (None = hold at `hi`).
    """
    def callback(phase_name, frame, total):
        if phase_name == "remux" or phase_name == "remux_bg":
            if remux_target is None:
                _update_job(job_id, phase=remux_phase, percent=hi)
            else:
                frac = frame / max(total, 1)
                pct = hi + frac * (remux_target - hi)
                _update_job(job_id, phase=remux_phase, percent=round(pct, 1))
            return

        pct = lo + (frame / max(total, 1)) * (hi - lo)
        _update_job(
            job_id,
            phase=phase,
            frame=frame,
            total=total,
            percent=round(pct, 1),
        )
    return callback


def _run_render(job_id, face_png, bg_preview, final_video):
    """Background render thread. Never raises — failures go into the job.

    A cancel request (POST /api/cancel/<job_id>) sets a flag on the job;
    it is picked up at frame boundaries (via `should_cancel`), between
    phases, and inside the ffmpeg remux (which terminates ffmpeg).
    """
    t0 = time.time()

    def should_cancel():
        return _is_cancelled(job_id)

    try:
        _update_job(job_id, status="rendering")

        if should_cancel():
            raise RenderCancelled("Render cancelled by user")

        log.info("[%s] Phase 2: face over background...", job_id)
        t1 = time.time()
        overlay.render_bg_preview(
            face_png=face_png,
            track_json=config.TRACKING_JSON,
            bg_video=config.BG_VIDEO,
            output_video=bg_preview,
            tmp_dir=config.TMP_DIR,
            progress_callback=_make_progress_callback(
                job_id, "overlay", *config.PHASE_2_WEIGHT,
                remux_phase="finalize_bg",
                remux_target=config.PHASE_3_WEIGHT[0],
            ),
            should_cancel=should_cancel,
        )
        log.info("[%s] Phase 2 done in %.1fs", job_id, time.time() - t1)

        if should_cancel():
            raise RenderCancelled("Render cancelled by user")

        log.info("[%s] Phase 3: composite + audio...", job_id)
        t2 = time.time()
        composite.render_final(
            bg_video=bg_preview,
            fg_video=config.FG_VIDEO,
            output_video=final_video,
            tmp_dir=config.TMP_DIR,
            progress_callback=_make_progress_callback(
                job_id, "composite", *config.PHASE_3_WEIGHT,
                remux_phase="remux", remux_target=100,
            ),
            should_cancel=should_cancel,
        )
        log.info("[%s] Phase 3 done in %.1fs", job_id, time.time() - t2)

        # Terminal flip must be atomic with the cancel check: once status
        # is "done" the job can no longer be cancelled, so the two can
        # never race. Direct dict writes — jobs_lock is not reentrant,
        # so _update_job must not be called while holding it.
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                return
            if job.get("cancel_requested"):
                cancelled = True
            else:
                job.update(status="done", phase="done", percent=100)
                cancelled = False

        if cancelled:
            raise RenderCancelled("Render cancelled by user")

        log.info("[%s] Render complete in %.1fs", job_id, time.time() - t0)

        # The result video is kept until /api/result serves it.
        for path in (face_png, bg_preview):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    except RenderCancelled:
        log.info("[%s] Render cancelled", job_id)
        _update_job(job_id, status="cancelled", error=None)
        for path in (face_png, bg_preview, final_video):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    except Exception as exc:
        log.exception("[%s] Render failed", job_id)
        _update_job(job_id, status="error", error=str(exc))
        for path in (face_png, bg_preview, final_video):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


# ----------------------------
# Routes
# ----------------------------
@app.route("/generate", methods=["POST"])
def generate():
    """Accept a face.png upload, start the render, return a job_id."""
    config.ensure_dirs()

    if "face" not in request.files:
        log.warning("POST /generate: no 'face' field in request")
        return jsonify({"error": "No face image uploaded."}), 400

    face_file = request.files["face"]

    if not face_file.filename:
        log.warning("POST /generate: empty file field")
        return jsonify({"error": "No face image uploaded."}), 400

    # Only one active render at a time: OpenCV + ffmpeg are CPU-heavy,
    # and parallel renders would starve a small instance.
    with jobs_lock:
        active_renders = sum(
            1 for job in jobs.values()
            if job["status"] in ("starting", "rendering")
            and not job.get("cancel_requested")
        )
    if active_renders >= MAX_ACTIVE_RENDERS:
        log.warning("POST /generate: rejected (%d active renders)",
                    active_renders)
        return jsonify({"error": RENDER_BUSY_MESSAGE}), 429

    job_id = uuid.uuid4().hex[:8]
    face_png = os.path.join(config.UPLOADS_DIR, job_id + "_face.png")
    bg_preview = os.path.join(config.TMP_DIR, job_id + "_bg_preview.mp4")
    final_video = os.path.join(config.TMP_DIR, job_id + "_final.mp4")

    try:
        face_file.save(face_png)

        # Reject anything that is not a real PNG (magic-byte check).
        with open(face_png, "rb") as f:
            head = f.read(8)
        if head != b"\x89PNG\r\n\x1a\n":
            try:
                os.remove(face_png)
            except OSError:
                pass
            log.warning("[%s] Rejected non-PNG upload", job_id)
            return jsonify({
                "error": "Only PNG images are supported — please upload a PNG file."
            }), 400

        log.info("[%s] Render started (upload: %s, %d bytes)",
                 job_id, face_file.filename, os.path.getsize(face_png))
    except OSError as exc:
        log.exception("[%s] Could not save upload", job_id)
        return jsonify({"error": "Could not save upload: %s" % exc}), 500

    # Register the job BEFORE starting the thread so the first poll
    # can never 404. Re-check for an active render under the lock: a
    # render may have started while the upload was being saved.
    with jobs_lock:
        busy = sum(
            1 for job in jobs.values()
            if job["status"] in ("starting", "rendering")
            and not job.get("cancel_requested")
        ) >= MAX_ACTIVE_RENDERS

        if not busy:
            jobs[job_id] = {
                "status": "starting",
                "phase": "",
                "frame": 0,
                "total": 0,
                "percent": 0,
                "error": None,
                "final_video": final_video,
                "cancel_requested": False,
                "eta_history": [],
            }
            _evict_old_jobs_locked()

    if busy:
        # Lost the race to a concurrent request — drop the saved upload.
        try:
            os.remove(face_png)
        except OSError:
            pass
        log.warning("[%s] Rejected: another render is active", job_id)
        return jsonify({"error": RENDER_BUSY_MESSAGE}), 429

    try:
        threading.Thread(
            target=_run_render,
            args=(job_id, face_png, bg_preview, final_video),
            daemon=True,
        ).start()
    except Exception as exc:
        # Never leave a job stuck in "starting" — the busy check counts
        # it as active and would 429-reject every later render forever.
        log.exception("[%s] Could not start render thread", job_id)
        with jobs_lock:
            jobs.pop(job_id, None)
        for path in (face_png, bg_preview, final_video):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        return jsonify({"error": "Could not start render: %s" % exc}), 500

    return jsonify({"job_id": job_id}), 202


@app.route("/api/progress/<job_id>")
def progress(job_id):
    """Current render state for the browser's progress bar."""
    with jobs_lock:
        job = jobs.get(job_id)
        history = list(job.get("eta_history", [])) if job else []

    if job is None:
        return jsonify({"error": "Unknown job."}), 404

    return jsonify({
        "status": job["status"],
        "phase": job.get("phase", ""),
        "frame": job.get("frame", 0),
        "total": job.get("total", 0),
        "percent": job.get("percent", 0),
        "eta": _estimate_eta(history, job.get("percent", 0)),
        "error": job.get("error"),
    })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    """Request cancellation of a running render.

    Cooperative: sets a flag the render thread checks at frame
    boundaries (and inside the ffmpeg remux). Returns 409 when the
    render already finished, so the browser can keep polling for the
    result instead.
    """
    with jobs_lock:
        job = jobs.get(job_id)

        if job is None:
            return jsonify({"error": "Unknown job."}), 404

        if job["status"] in ("done", "error", "cancelled"):
            return jsonify({"error": "Render already finished."}), 409

        job["cancel_requested"] = True

    log.info("[%s] Cancel requested", job_id)
    return jsonify({"status": "cancelling"}), 200


@app.route("/api/result/<job_id>")
def result(job_id):
    """Return the finished video (409 while rendering, 500 on failure)."""
    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Unknown job."}), 404

    status = job["status"]
    final_video = job.get("final_video")

    if status == "rendering" or status == "starting":
        return jsonify({"error": "Still rendering."}), 409

    if status == "cancelled":
        return jsonify({"error": "Render was cancelled."}), 410

    if status == "error":
        return jsonify({"error": job.get("error", "Render failed.")}), 500

    # status == "done": buffer the bytes, then delete file + job entry.
    try:
        with open(final_video, "rb") as f:
            video_bytes = f.read()
    except OSError as exc:
        log.error("[%s] Result file missing: %s", job_id, exc)
        with jobs_lock:
            jobs.pop(job_id, None)
        return jsonify({"error": "Result file missing: %s" % exc}), 500

    try:
        if os.path.exists(final_video):
            os.remove(final_video)
    except OSError:
        pass

    with jobs_lock:
        jobs.pop(job_id, None)

    log.info("[%s] Served final.mp4 (%d bytes)", job_id, len(video_bytes))

    return Response(
        video_bytes,
        mimetype="video/mp4",
        headers={
            # inline (not attachment): the page fetches this and plays it in
            # the in-page <video>; attachment makes some mobile browsers
            # grab the file into the download manager instead.
            # The Download button saves via its own blob URL, so it keeps
            # working without an attachment header.
            "Content-Disposition": 'inline; filename="final.mp4"',
            "Cache-Control": "no-store",
        },
    )


if __name__ == "__main__":
    # Wipe leftovers from previous sessions, then start.
    for directory in (config.UPLOADS_DIR, config.TMP_DIR):
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass

    log.info("Starting server on http://0.0.0.0:5000")
    # 0.0.0.0 so phones/tablets on the same Wi-Fi can test at
    # http://<PC-LAN-IP>:5000 (find it with `ipconfig`).
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
