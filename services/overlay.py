"""Phase 2: overlay the face PNG onto the background video.

Adapted from `phase 2 Put tracking in bg/track_test_apply.py`.
"""

import json
import os

import cv2
import numpy as np

import config
from services import RenderCancelled, media


def _load_tracking(track_json):
    """Read the tracking JSON (tolerates the UTF-8 BOM)."""
    with open(track_json, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    if "faces" in data and len(data["faces"]) > 0:
        face_frames = data["faces"][0]["frames"]
    elif "frames" in data:
        face_frames = data["frames"]
    else:
        raise ValueError("No tracking data found in JSON.")

    return {
        int(item["frame"]): item
        for item in face_frames
    }


def _overlay(frame, png, x, y, scale_x, scale_y, rotation, opacity):
    """Alpha-composite a rotated/scaled RGBA PNG centered at (x, y)."""
    if opacity <= 0:
        return frame

    h, w = png.shape[:2]

    new_w = max(1, int(w * scale_x / 100))
    new_h = max(1, int(h * scale_y / 100))

    png = cv2.resize(png, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    center = (new_w / 2, new_h / 2)

    # OpenCV positive rotation is CCW; AE rotation is CW — sign flip is intentional.
    M = cv2.getRotationMatrix2D(center, -rotation, 1)

    cos = abs(M[0, 0])
    sin = abs(M[0, 1])

    bound_w = int(new_h * sin + new_w * cos)
    bound_h = int(new_h * cos + new_w * sin)

    M[0, 2] += bound_w / 2 - center[0]
    M[1, 2] += bound_h / 2 - center[1]

    png = cv2.warpAffine(
        png,
        M,
        (bound_w, bound_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )

    x = int(x - bound_w / 2)
    y = int(y - bound_h / 2)

    H, W = frame.shape[:2]

    if x >= W or y >= H:
        return frame

    if x + bound_w <= 0 or y + bound_h <= 0:
        return frame

    x1 = max(x, 0)
    y1 = max(y, 0)
    x2 = min(x + bound_w, W)
    y2 = min(y + bound_h, H)

    px1 = x1 - x
    py1 = y1 - y
    px2 = px1 + (x2 - x1)
    py2 = py1 + (y2 - y1)

    crop = png[py1:py2, px1:px2]

    alpha = crop[:, :, 3].astype(np.float32) / 255.0
    alpha *= opacity
    alpha = alpha[:, :, None]

    frame_crop = frame[y1:y2, x1:x2].astype(np.float32)
    png_crop = crop[:, :, :3].astype(np.float32)

    frame[y1:y2, x1:x2] = (
        png_crop * alpha + frame_crop * (1 - alpha)
    ).astype(np.uint8)

    return frame


def render_bg_preview(face_png, track_json, bg_video, output_video, tmp_dir,
                      progress_callback=None, should_cancel=None):
    """Render the face animation over the background video.

    Writes an OpenCV mp4v temp video, then remuxes the background's
    audio via ffmpeg. Returns the output path.

    `progress_callback(phase, frame, total)` is invoked per frame
    (phase "overlay") and once before the audio remux (phase "remux").
    It is best-effort: exceptions inside it never abort the render.

    `should_cancel()` (optional) is checked per frame; when it returns
    True the render aborts by raising `RenderCancelled`.
    """
    track_by_frame = _load_tracking(track_json)

    cap = cv2.VideoCapture(bg_video)

    if not cap.isOpened():
        raise ValueError("Cannot open background video: %s" % bg_video)

    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    face = cv2.imread(face_png, cv2.IMREAD_UNCHANGED)

    if face is None:
        cap.release()
        raise ValueError("Cannot open face image: %s" % face_png)

    if face.ndim != 3 or face.shape[2] != 4:
        cap.release()
        raise ValueError(
            "The face image must be a PNG with transparency "
            "(a 4-channel RGBA export from the tool)."
        )

    # Unique temp name per output so concurrent renders cannot collide.
    base = os.path.splitext(os.path.basename(output_video))[0]
    temp_video = os.path.join(tmp_dir, base + "_temp.mp4")

    writer = cv2.VideoWriter(
        temp_video,
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )

    frame_no = 0

    try:
        # The mp4v writer finalizes its moov atom only on release(), and
        # ffmpeg reads the temp file right after — so the writer must be
        # released before the remux. The inner finally guarantees that
        # even when the frame loop is cancelled mid-render.
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    raise RenderCancelled("Render cancelled by user")

                ret, frame = cap.read()

                if not ret:
                    break

                if frame_no in track_by_frame:
                    t = track_by_frame[frame_no]

                    opacity = float(t.get("opacity", 1))

                    if opacity > 0:
                        frame = _overlay(
                            frame,
                            face,
                            float(t.get("x", 0)),
                            float(t.get("y", 0)),
                            float(t.get("scaleX", 100)),
                            float(t.get("scaleY", 100)),
                            float(t.get("rotation", 0)),
                            opacity,
                        )

                writer.write(frame)
                frame_no += 1

                if progress_callback is not None:
                    try:
                        progress_callback("overlay", frame_no, total_frames)
                    except Exception:
                        pass  # progress reporting is best-effort
        finally:
            cap.release()
            writer.release()

        if progress_callback is not None:
            def _remux_progress(frac):
                try:
                    progress_callback("remux_bg", frac, 1)
                except Exception:
                    pass  # progress reporting is best-effort

            # Mark the remux start: ffmpeg may finish too fast to report
            # intermediate values, so the UI still sees the phase change.
            try:
                progress_callback("remux_bg", 0, 1)
            except Exception:
                pass
        else:
            _remux_progress = None

        media.remux_audio(
            temp_video, bg_video, output_video,
            remux_progress=_remux_progress,
            duration=(total_frames / fps) if fps else None,
            should_cancel=should_cancel,
        )
    finally:
        # Clean up the temp video on every exit path (normal completion,
        # failure, or cancellation).
        try:
            if os.path.exists(temp_video):
                os.remove(temp_video)
        except OSError:
            pass

    return output_video
