"""Central configuration for the website.

All paths, video specs, and render tuning knobs live here so the
pipeline modules never hard-code anything.
"""

import os

# ----------------------------
# Paths
# ----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ASSETS_DIR = os.path.join(BASE_DIR, "assets")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
TMP_DIR = os.path.join(BASE_DIR, "tmp")

TRACKING_JSON = os.path.join(ASSETS_DIR, "tracking", "track_result.json")
BG_VIDEO = os.path.join(ASSETS_DIR, "background", "BG.mp4")
FG_VIDEO = os.path.join(ASSETS_DIR, "foreground", "fg_preview.mp4")

# ----------------------------
# Video spec (verified via ffprobe on the source assets)
# ----------------------------
FPS = 24
WIDTH = 568
HEIGHT = 320

# ----------------------------
# Render tuning knobs (phase 2)
# ----------------------------
# Nothing to tune in phase 2: it uses the tracking JSON as-is.

# ----------------------------
# Render tuning knobs (phase 3 — brightness matte)
# ----------------------------
BLACK_CUTOFF = 5        # pixels brighter than this are treated as foreground
FEATHER_RANGE = 35      # brightness range over which alpha ramps 0 -> 1
BLUR_KERNEL = (5, 5)    # alpha smoothing kernel
ERODE_KERNEL = 3        # shrink alpha edge to remove halo
ERODE_ITERATIONS = 1
ALPHA_SAFE = 0.05       # floor used when un-multiplying black contamination

# ----------------------------
# Progress reporting (percent ranges per render phase)
# ----------------------------
PHASE_2_WEIGHT = (0, 25)    # overlay loop covers 0–25%
PHASE_3_WEIGHT = (40, 90)   # composite loop covers 40–90%; the phase-2
                            # audio remux glides 25→40 so the ETA estimate
                            # never stalls on a frozen percentage.
                            # Spans are sized to typical Render phase
                            # durations (remuxes are slow there) so the
                            # ETA stays sane across phase changes.

# ----------------------------
# ffmpeg delivery settings
# ----------------------------
FFMPEG_VIDEO_CODEC = "libx264"
FFMPEG_PRESET = "fast"
FFMPEG_CRF = "23"
FFMPEG_AUDIO_CODEC = "aac"
FFMPEG_FASTSTART = True


def ensure_dirs():
    """Create the runtime directories (uploads/tmp) if missing."""
    for directory in (UPLOADS_DIR, TMP_DIR):
        os.makedirs(directory, exist_ok=True)
