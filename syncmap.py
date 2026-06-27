"""
Multi-clip sync map.

The external recorder runs continuously for the whole show (one .wav). Cameras
start/stop, so the footage arrives as several .mov files, each covering a
different slice of the show. We sync EACH clip to the master WAV independently,
giving each its own offset, then know which clip covers which WAV-time range.

Coverage in WAV time (mov_time = wav_time + offset, so wav_time = mov_time - offset):
    clip covers WAV [ -offset , duration - offset ]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import audio_sync
import media
from song_splitter import Song


@dataclass
class ClipSync:
    path: str
    offset: float        # L: mov_time = wav_time + offset
    duration: float      # clip length (s)
    confidence: float
    width: int = 0
    height: int = 0
    scratch_path: str = ""   # mono scratch wav (for per-song drift refine)
    label: str = ""

    @property
    def wav_start(self) -> float:
        return -self.offset

    @property
    def wav_end(self) -> float:
        return self.duration - self.offset

    def coverage(self, a: float, b: float) -> float:
        """Fraction of WAV window [a,b] this clip covers."""
        if b <= a:
            return 0.0
        lo, hi = max(a, self.wav_start), min(b, self.wav_end)
        return max(0.0, hi - lo) / (b - a)


def build_sync_map(video_paths: list[str], master_wav_path: str, scratch_dir: str,
                   refine: bool = True, on_progress=None) -> list[ClipSync]:
    """Sync every clip to the master WAV. Returns clips sorted by WAV start time."""
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    master_mono = scratch / "master.mono.wav"
    media.extract_audio(master_wav_path, master_mono)

    clips: list[ClipSync] = []
    for vp in video_paths:
        info = media.probe(vp)
        sc = scratch / (Path(vp).stem + ".scratch.wav")
        media.extract_audio(vp, sc)
        res = audio_sync.estimate_offset(str(sc), str(master_mono), refine=refine)
        clip = ClipSync(
            path=str(media.resolve_source(vp)),
            offset=res.offset, duration=info.duration, confidence=res.confidence,
            width=info.width or 0, height=info.height or 0,
            scratch_path=str(sc), label=Path(vp).stem,
        )
        clips.append(clip)
        if on_progress:
            on_progress(clip)

    clips.sort(key=lambda c: c.wav_start)
    return clips


def best_clip(clips: list[ClipSync], song: Song) -> ClipSync | None:
    """The clip covering the most of this song (tie-break on sync confidence)."""
    best, best_cov = None, 0.0
    for c in clips:
        cov = c.coverage(song.start, song.end)
        if cov > best_cov + 1e-6 or (abs(cov - best_cov) <= 1e-6 and best and c.confidence > best.confidence):
            best, best_cov = c, cov
    return best if best_cov > 0 else None


def write_sync_map(clips: list[ClipSync], path: str, master_wav: str) -> None:
    payload = {
        "master_wav": str(master_wav),
        "clips": [asdict(c) for c in clips],
    }
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def read_sync_map(path: str) -> tuple[list[ClipSync], str]:
    data = json.loads(Path(path).read_text())
    clips = [ClipSync(**c) for c in data["clips"]]
    return clips, data.get("master_wav", "")
