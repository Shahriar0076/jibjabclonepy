"""Render pipeline services (phases 2 and 3 of the video workflow)."""


class RenderCancelled(Exception):
    """Raised by pipeline services when the render is cancelled.

    The render thread checks a per-job cancel flag and signals the
    services via a `should_cancel` callable; the services raise this
    exception so the job ends with status "cancelled" (not "error")
    and all partial files are cleaned up.
    """
