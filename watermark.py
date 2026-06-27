"""
PNG watermark overlay (ffmpeg filtergraph fragments).

Scales the logo to a fraction of the video width, applies opacity, and positions
it in a corner/center with a margin. Position is expressed with ffmpeg overlay
variables so we don't need the watermark's scaled height.
"""

from __future__ import annotations

POSITIONS = {
    "tl": "{m}:{m}",
    "tr": "main_w-overlay_w-{m}:{m}",
    "bl": "{m}:main_h-overlay_h-{m}",
    "br": "main_w-overlay_w-{m}:main_h-overlay_h-{m}",
    "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
}


def resolve_mode(mode: str, vw: int, vh: int, ww: int, wh: int) -> str:
    """
    Decide badge vs fullframe.

    A full-frame watermark is a PNG the same size as the video (logo positioned
    inside a transparent full-frame canvas) — overlay it 1:1, do NOT shrink it.
    A badge is just the logo graphic — scale it to a fraction of the video and
    drop it in a corner.
    """
    if mode != "auto":
        return mode
    if not (ww and wh and vw and vh):
        return "badge"
    same_aspect = abs((ww / wh) - (vw / vh)) < 0.05
    near_full = ww >= vw * 0.6
    return "fullframe" if (same_aspect and near_full) else "badge"


def overlay_filter(video_width: int, video_height: int, wm_width: int, wm_height: int,
                   wm_input: int = 2, *, mode: str = "auto",
                   position: str = "br", scale: float = 0.12,
                   opacity: float = 0.85, margin_frac: float = 0.025,
                   video_label: str = "0:v", out_label: str = "v") -> str:
    """
    Build a -filter_complex string that overlays input #`wm_input` (the PNG) onto
    `video_label`, producing `[out_label]`.

    mode        : "auto" | "badge" | "fullframe"
    scale       : (badge) watermark width as a fraction of video width
    opacity     : 0..1 (multiplies the PNG's own alpha, so transparency is kept)
    margin_frac : (badge) margin as a fraction of video width
    """
    if position not in POSITIONS:
        raise ValueError(f"position must be one of {list(POSITIONS)}")

    mode = resolve_mode(mode, video_width, video_height, wm_width, wm_height)
    alpha = f"format=rgba,colorchannelmixer=aa={opacity:.3f}"

    if mode == "fullframe":
        # stretch the watermark to the exact video frame and overlay at origin
        return (
            f"[{wm_input}]{alpha},scale={video_width}:{video_height}[wm];"
            f"[{video_label}][wm]overlay=0:0:format=auto[{out_label}]"
        )

    wm_w = max(1, round(video_width * scale))
    margin = max(0, round(video_width * margin_frac))
    pos = POSITIONS[position].format(m=margin)
    return (
        f"[{wm_input}]{alpha},scale={wm_w}:-1[wm];"
        f"[{video_label}][wm]overlay={pos}:format=auto[{out_label}]"
    )
