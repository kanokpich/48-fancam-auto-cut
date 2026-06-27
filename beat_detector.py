"""
Audio analysis: detect beats, onsets (applause/drops), and silence.
Returns timestamps in seconds.
"""

import numpy as np
import librosa


def extract_audio(video_path: str, sr: int = 22050) -> tuple[np.ndarray, int]:
    """Load audio directly from video file via librosa (uses ffmpeg under the hood)."""
    y, sr = librosa.load(video_path, sr=sr, mono=True)
    return y, sr


def detect_beats(y: np.ndarray, sr: int) -> tuple[float, np.ndarray]:
    """Return (tempo_bpm, beat_timestamps_in_seconds)."""
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    return float(tempo), beat_times


def detect_onsets(y: np.ndarray, sr: int, threshold: float = 0.5) -> np.ndarray:
    """
    Detect sudden loud events (applause, music drops, chants).
    threshold: sensitivity 0.0–1.0; higher = only the loudest hits.
    Returns timestamps in seconds.
    """
    onset_frames = librosa.onset.onset_detect(
        y=y, sr=sr, units="frames",
        delta=threshold, wait=10,  # wait=10 frames minimum between onsets
    )
    return librosa.frames_to_time(onset_frames, sr=sr)


def detect_silence(y: np.ndarray, sr: int, top_db: float = 40.0) -> list[tuple[float, float]]:
    """
    Find silent intervals. Returns list of (start_sec, end_sec) tuples.
    top_db: silence threshold in dB below peak; lower = stricter silence definition.
    """
    intervals = librosa.effects.split(y, top_db=top_db)
    silent_gaps = []
    for i in range(1, len(intervals)):
        gap_start = librosa.samples_to_time(intervals[i - 1][1], sr=sr)
        gap_end = librosa.samples_to_time(intervals[i][0], sr=sr)
        if gap_end - gap_start > 0.1:  # ignore gaps shorter than 100ms
            silent_gaps.append((float(gap_start), float(gap_end)))
    return silent_gaps


def get_energy_peaks(y: np.ndarray, sr: int, top_n: int = 20) -> np.ndarray:
    """
    Find the top-N highest-energy moments — useful for highlight reel selection.
    Returns timestamps in seconds.
    """
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    # Get top N peaks with minimum spacing of 2 seconds
    min_spacing = int(2.0 * sr / hop_length)
    peak_indices = []
    sorted_indices = np.argsort(rms)[::-1]

    for idx in sorted_indices:
        if len(peak_indices) >= top_n:
            break
        if all(abs(idx - p) > min_spacing for p in peak_indices):
            peak_indices.append(idx)

    peak_indices.sort()
    return frame_times[peak_indices]
