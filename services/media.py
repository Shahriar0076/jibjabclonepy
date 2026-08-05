"""Shared media helpers: ffmpeg remux and ffprobe verification."""

import subprocess

import config
from services import RenderCancelled


def remux_audio(video_path, audio_source, output_path,
                remux_progress=None, duration=None, should_cancel=None):
    """Re-encode video from `video_path` and copy audio from `audio_source`.

    OpenCV's mp4v writer cannot write AAC audio, so every render goes
    through ffmpeg: video from the temp file, audio from the source
    background video.

    `remux_progress(frac)` is invoked with 0..1 as ffmpeg progresses
    (parsed live from its `-progress` output, gated by `duration` in
    seconds). It is best-effort: exceptions never abort the remux.

    `should_cancel()` (optional) is checked as ffmpeg reports progress;
    when it returns True the ffmpeg process is terminated and
    `RenderCancelled` is raised.
    """
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-i", audio_source,
        "-c:v", config.FFMPEG_VIDEO_CODEC,
        "-preset", config.FFMPEG_PRESET,
        "-crf", config.FFMPEG_CRF,
        "-c:a", config.FFMPEG_AUDIO_CODEC,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
    ]

    if config.FFMPEG_FASTSTART:
        command += ["-movflags", "+faststart"]

    command += ["-progress", "pipe:1", "-nostats", output_path]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Read ffmpeg's progress lines as they arrive (out_time_us).
        cancelled = False
        for line in process.stdout:
            line = line.strip()

            if should_cancel is not None and should_cancel():
                cancelled = True
                process.terminate()
                break

            if not line.startswith("out_time_us="):
                continue

            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue

            if remux_progress is not None and duration and duration > 0:
                frac = min(us / (duration * 1_000_000), 1.0)
                try:
                    remux_progress(frac)
                except Exception:
                    pass  # progress reporting is best-effort
    finally:
        if process.stdout:
            process.stdout.close()

    stderr = process.stderr.read()
    if process.stderr:
        process.stderr.close()

    returncode = process.wait()

    if cancelled:
        raise RenderCancelled("Render cancelled by user")

    if returncode != 0:
        raise RuntimeError(
            "ffmpeg failed:\n%s" % (stderr[-2000:] if stderr else "")
        )


def probe_video(path):
    """Return (width, height, fps) of a video via ffprobe, or None."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    parts = result.stdout.strip().split(",")

    if len(parts) != 3:
        return None

    width, height, rate = parts

    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den) if den else float(num)
        return int(width), int(height), fps
    except ValueError:
        return None
