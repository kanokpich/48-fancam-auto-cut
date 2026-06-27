"""
Generate cut lists from beat/onset data.
Produces a list of (start_sec, end_sec) segments ready for export.
"""

import numpy as np


def cuts_from_beats(
    beat_times: np.ndarray,
    duration: float,
    every_n_beats: int = 2,
    min_clip_length: float = 1.0,
) -> list[tuple[float, float]]:
    """
    Cut on every N-th beat.
    every_n_beats=1 → every beat (very fast); 2 → every other beat; 4 → every bar.
    """
    cut_points = [0.0] + list(beat_times[::every_n_beats]) + [duration]
    segments = []
    for i in range(len(cut_points) - 1):
        start, end = cut_points[i], cut_points[i + 1]
        if end - start >= min_clip_length:
            segments.append((round(start, 3), round(end, 3)))
    return segments


def cuts_from_onsets(
    onset_times: np.ndarray,
    duration: float,
    pre_roll: float = 0.1,
    clip_length: float = 3.0,
) -> list[tuple[float, float]]:
    """
    Cut to a fixed-length clip around each onset (energy peak / applause).
    pre_roll: how many seconds before the onset to start.
    """
    segments = []
    for t in onset_times:
        start = max(0.0, t - pre_roll)
        end = min(duration, start + clip_length)
        segments.append((round(start, 3), round(end, 3)))
    return _merge_overlapping(segments)


def cuts_from_silence(
    silent_gaps: list[tuple[float, float]],
    duration: float,
    min_clip_length: float = 1.5,
) -> list[tuple[float, float]]:
    """
    Cut at silence boundaries — natural edit points.
    """
    boundaries = [0.0]
    for gap_start, gap_end in silent_gaps:
        boundaries.append(gap_start)
        boundaries.append(gap_end)
    boundaries.append(duration)
    boundaries = sorted(set(boundaries))

    segments = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        if end - start >= min_clip_length:
            segments.append((round(start, 3), round(end, 3)))
    return segments


def select_highlights(
    segments: list[tuple[float, float]],
    energy_peaks: np.ndarray,
    top_n: int = 10,
) -> list[tuple[float, float]]:
    """
    From a cut list, pick the top-N segments that contain the highest-energy moments.
    """
    scored = []
    for seg in segments:
        start, end = seg
        peaks_in = np.sum((energy_peaks >= start) & (energy_peaks < end))
        scored.append((peaks_in, seg))
    scored.sort(key=lambda x: (-x[0], x[1][0]))  # sort by peak count, then time
    return [seg for _, seg in scored[:top_n]]


def _merge_overlapping(segments: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not segments:
        return []
    segments = sorted(segments)
    merged = [segments[0]]
    for start, end in segments[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
