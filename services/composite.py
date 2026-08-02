"""Phase 3: composite the foreground over the background.

Adapted from `phase 3 Put fg and bg together/final.py`.
Uses a brightness-based alpha matte with edge cleanup and
black-contamination un-multiply.
"""

import os

import cv2
import numpy as np

import config
from services import media


def render_final(bg_video, fg_video, output_video, tmp_dir, progress_callback=None):
    """Composite fg (bright subject on black) over bg.

    Writes an OpenCV mp4v temp video, then remuxes the background's
    audio via ffmpeg. Returns the output path.

    `progress_callback(phase, frame, total)` is invoked per frame
    (phase "composite") and once before the audio remux (phase "remux").
    It is best-effort: exceptions inside it never abort the render.
    """
    bg = cv2.VideoCapture(bg_video)
    fg = cv2.VideoCapture(fg_video)

    if not bg.isOpened():
        raise ValueError("Cannot open background video: %s" % bg_video)

    if not fg.isOpened():
        bg.release()
        raise ValueError("Cannot open foreground video: %s" % fg_video)

    fps = bg.get(cv2.CAP_PROP_FPS)
    width = int(bg.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(bg.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(bg.get(cv2.CAP_PROP_FRAME_COUNT))

    # Unique temp name per output so concurrent renders cannot collide.
    base = os.path.splitext(os.path.basename(output_video))[0]
    temp_video = os.path.join(tmp_dir, base + "_temp.mp4")

    writer = cv2.VideoWriter(
        temp_video,
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )

    black_cutoff = config.BLACK_CUTOFF
    feather_range = config.FEATHER_RANGE
    blur_kernel = config.BLUR_KERNEL
    erode_kernel = config.ERODE_KERNEL
    erode_iters = config.ERODE_ITERATIONS
    alpha_safe = config.ALPHA_SAFE

    try:
        frame_no = 0

        while True:
            ret_bg, bg_frame = bg.read()
            ret_fg, fg_frame = fg.read()

            if not ret_bg or not ret_fg:
                break

            if fg_frame.shape[:2] != bg_frame.shape[:2]:
                fg_frame = cv2.resize(fg_frame, (width, height))

            bg_float = bg_frame.astype(np.float32)
            fg_float = fg_frame.astype(np.float32)

            # Smooth alpha from brightness
            brightness = np.max(fg_float, axis=2)

            alpha = np.clip(
                (brightness - black_cutoff) / feather_range,
                0.0,
                1.0,
            )

            # Clean alpha edge
            alpha = cv2.GaussianBlur(alpha, blur_kernel, 0)

            # Slightly shrink edge to remove halo
            alpha_u8 = (alpha * 255).astype(np.uint8)
            kernel = np.ones((erode_kernel, erode_kernel), np.uint8)
            alpha_u8 = cv2.erode(alpha_u8, kernel, iterations=erode_iters)
            alpha = alpha_u8.astype(np.float32) / 255.0

            alpha = cv2.GaussianBlur(alpha, blur_kernel, 0)
            alpha_3 = alpha[:, :, None]

            # Remove black contamination from foreground
            alpha_floor = np.maximum(alpha_3, alpha_safe)

            fg_clean = fg_float / alpha_floor
            fg_clean = np.clip(fg_clean, 0, 255)

            # Composite foreground over background
            result = (
                fg_clean * alpha_3 +
                bg_float * (1.0 - alpha_3)
            )

            result = np.clip(result, 0, 255).astype(np.uint8)

            writer.write(result)
            frame_no += 1

            if progress_callback is not None:
                try:
                    progress_callback("composite", frame_no, total_frames)
                except Exception:
                    pass  # progress reporting is best-effort
    finally:
        bg.release()
        fg.release()
        writer.release()

    if progress_callback is not None:
        def _remux_progress(frac):
            try:
                progress_callback("remux", frac, 1)
            except Exception:
                pass  # progress reporting is best-effort

        # Mark the remux start: ffmpeg may finish too fast to report
        # intermediate values, so the UI still sees the phase change.
        try:
            progress_callback("remux", 0, 1)
        except Exception:
            pass
    else:
        _remux_progress = None

    try:
        media.remux_audio(
            temp_video, bg_video, output_video,
            remux_progress=_remux_progress,
            duration=(total_frames / fps) if fps else None,
        )
    finally:
        # Clean up the temp video even if the remux fails.
        if os.path.exists(temp_video):
            os.remove(temp_video)

    return output_video
