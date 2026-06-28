"""
Song-boundary detection on the clean master WAV.

A live idol recording interleaves: songs (loud, strong steady beat) with MC talk,
waiting, and applause (no steady beat). We score each short window by:

  - energy        (gate out silence / quiet talk)
  - pulse clarity (how strong & steady the beat is -> music, not speech/applause)

then group sustained 'music' windows into songs, merge brief internal gaps
(quiet bridges), drop runs that are too short, and pad the edges.

This is a heuristic. It writes an EDITABLE songs.json you review before rendering.
No auto-detector is perfect on live audio; treat its output as a first draft.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import librosa
import numpy as np

HOP = 512


@dataclass
class Song:
    index: int
    start: float          # seconds, in WAV timeline
    end: float
    label: str = ""
    render_solo: bool = True   # if False: skip individual clip; still included in full

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class DetectParams:
    window: float = 4.0          # analysis window (s)
    step: float = 1.0            # window hop (s)
    min_song: float = 60.0       # discard music runs shorter than this (s)
    merge_gap: float = 12.0      # bridge gaps shorter than this within a song (s)
    pad_start: float = 2.0       # lead-in before music kicks in (s)
    pad_end: float = 2.0         # tail after music ends (s)
    pulse_thresh: float = 0.30   # min pulse clarity to call a window 'music'
    energy_floor_db: float = 25.0  # window must be within this many dB of the loudest


def _pulse_clarity(env: np.ndarray, sr: int, hop: int,
                   bpm_lo: float = 60, bpm_hi: float = 200) -> float:
    """
    Autocorrelation-based beat strength of an onset-envelope slice.
    Returns the strongest normalized periodicity in the plausible tempo band.
    Music -> high; speech / applause / silence -> low.
    """
    if env.size < 8 or env.std() < 1e-6:
        return 0.0
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[e.size - 1:]
    if ac[0] <= 0:
        return 0.0
    ac = ac / ac[0]
    fps = sr / hop
    lag_lo = max(1, int(fps * 60.0 / bpm_hi))
    lag_hi = min(len(ac) - 1, int(fps * 60.0 / bpm_lo))
    if lag_hi <= lag_lo:
        return 0.0
    return float(np.clip(ac[lag_lo:lag_hi].max(), 0.0, 1.0))


def detect_songs(wav_path: str, params: DetectParams | None = None,
                 sr: int = 22050, progress=None) -> tuple[list[Song], np.ndarray]:
    """
    Returns (songs, feature_rows). feature_rows is an (N,4) array of
    [time, energy_db, pulse_clarity, is_music] for inspection / tuning.

    progress(frac) : optional 0..1 callback for the analysis loop.
    """
    p = params or DetectParams()
    y, sr = librosa.load(wav_path, sr=sr, mono=True)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    duration = len(y) / sr

    win_n = int(p.window * sr)
    step_n = int(p.step * sr)
    env_fps = sr / HOP

    starts = list(range(0, max(1, len(y) - win_n), step_n))
    times, energies, pulses = [], [], []
    for idx, start in enumerate(starts):
        seg = y[start:start + win_n]
        t = start / sr
        rms = np.sqrt(np.mean(seg ** 2)) + 1e-9
        e_lo = int((start / sr) * env_fps)
        e_hi = int(((start + win_n) / sr) * env_fps)
        pulse = _pulse_clarity(env[e_lo:e_hi], sr, HOP)
        times.append(t)
        energies.append(20 * np.log10(rms))
        pulses.append(pulse)
        if progress and idx % 16 == 0:
            progress(idx / len(starts))
    if progress:
        progress(1.0)

    times = np.array(times)
    energies = np.array(energies)
    pulses = np.array(pulses)

    # energy gate, relative to the loudest window (robust to absolute level)
    e_gate = energies.max() - p.energy_floor_db
    is_music = (pulses >= p.pulse_thresh) & (energies >= e_gate)

    songs = _group(times, is_music, p, duration)
    rows = np.column_stack([times, energies, pulses, is_music.astype(float)])
    return songs, rows


def _group(times: np.ndarray, is_music: np.ndarray, p: DetectParams,
           duration: float) -> list[Song]:
    # collect raw runs of music windows
    runs: list[list[float]] = []
    in_run = False
    for i, m in enumerate(is_music):
        if m and not in_run:
            runs.append([times[i], times[i]])
            in_run = True
        elif m:
            runs[-1][1] = times[i]
        else:
            in_run = False

    if not runs:
        return []

    # merge runs separated by short gaps
    merged = [runs[0]]
    for s, e in runs[1:]:
        if s - merged[-1][1] <= p.merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    # drop short runs, pad, clamp, number
    songs: list[Song] = []
    for s, e in merged:
        if e - s + p.window < p.min_song:
            continue
        start = max(0.0, s - p.pad_start)
        end = min(duration, e + p.window + p.pad_end)
        songs.append(Song(index=len(songs) + 1, start=round(start, 2),
                          end=round(end, 2), label=f"song{len(songs) + 1:02d}"))
    return songs


# ── time formats ─────────────────────────────────────────────────────────────

def parse_time(v) -> float:
    """
    Accept any of:
      125 / 125.0        -> seconds (number)
      "125" / "125.5"    -> seconds (numeric string)
      "2:05"             -> M:SS        = 125 s
      "1:02:05"          -> H:MM:SS     = 3725 s
      "2:05.5"           -> fractional seconds allowed
    """
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if ":" not in s:
        return float(s)
    parts = s.split(":")
    if len(parts) == 2:
        m, sec = parts
        return int(m) * 60 + float(sec)
    if len(parts) == 3:
        h, m, sec = parts
        return int(h) * 3600 + int(m) * 60 + float(sec)
    raise ValueError(f"bad time value: {v!r}")


def format_time(t: float) -> str:
    """Seconds -> 'M:SS' (under an hour) or 'H:MM:SS', rounded to whole seconds."""
    total = int(round(max(0.0, t)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── editable JSON I/O ────────────────────────────────────────────────────────

def write_songs(songs: list[Song], path: str, meta: dict | None = None) -> None:
    payload = {
        "_comment": "เวลาใส่เป็น น:วว หรือ ชม:นน:วว ก็ได้ (เช่น 2:05 หรือ 1:02:05) "
                    "หรือเป็นวินาทีเฉยๆก็ได้. แก้ start/end แล้วรัน render. ลบเพลงที่ไม่ต้องการออกได้.",
        "meta": meta or {},
        "songs": [
            {"index": s.index, "start": format_time(s.start),
             "end": format_time(s.end), "label": s.label,
             "render_solo": s.render_solo}
            for s in songs
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def read_songs(path: str) -> list[Song]:
    data = json.loads(Path(path).read_text())
    songs = []
    for s in data["songs"]:
        songs.append(Song(
            index=int(s.get("index", len(songs) + 1)),
            start=parse_time(s["start"]),
            end=parse_time(s["end"]),
            label=s.get("label", f"song{len(songs) + 1:02d}"),
            render_solo=bool(s.get("render_solo", True)),
        ))
    return songs
