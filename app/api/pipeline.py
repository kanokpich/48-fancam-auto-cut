"""Sync, detect, waveform peaks, and songs CRUD endpoints."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import syncmap as sm
import song_splitter as ss
from app import jobs as _jobs

router = APIRouter()


# ── Sync ──────────────────────────────────────────────────────────────────────

class SyncRequest(BaseModel):
    movs: list[str]
    wav: str
    out_dir: str


def _run_sync(job: _jobs.Job, req: SyncRequest) -> None:
    try:
        job.status = "running"
        job.push("start", {"total_clips": len(req.movs)})
        scratch = str(Path(req.out_dir) / ".scratch")
        done = [0]

        def _on_clip(clip):
            done[0] += 1
            job.push("clip_done", {
                "label": clip.label, "offset": round(clip.offset, 3),
                "confidence": round(clip.confidence, 2),
                "wav_start": round(clip.wav_start, 1),
                "wav_end": round(clip.wav_end, 1),
                "n": done[0],
            })

        clips = sm.build_sync_map(req.movs, req.wav, scratch, on_progress=_on_clip)
        sync_path = str(Path(req.out_dir) / "sync.json")
        sm.write_sync_map(clips, sync_path, req.wav)
        result = {
            "sync_json": sync_path,
            "clips": [
                {
                    "label": c.label, "path": c.path,
                    "offset": round(c.offset, 3), "confidence": round(c.confidence, 2),
                    "wav_start": round(c.wav_start, 1), "wav_end": round(c.wav_end, 1),
                }
                for c in clips
            ],
        }
        job.result = result
        job.status = "done"
        job.push("done", result)
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.push("failed", str(e))


@router.post("/sync")
def start_sync(req: SyncRequest):
    Path(req.out_dir).mkdir(parents=True, exist_ok=True)
    job = _jobs.create("sync")
    threading.Thread(target=_run_sync, args=(job, req), daemon=True).start()
    return {"job_id": job.id}


# ── Detect ────────────────────────────────────────────────────────────────────

class DetectRequest(BaseModel):
    wav: str
    out_dir: str
    min_song: float = 60.0
    merge_gap: float = 12.0
    pad_start: float = 2.0
    pad_end: float = 2.0
    energy_floor_db: float = 25.0
    pulse_thresh: float = 0.30


def _run_detect(job: _jobs.Job, req: DetectRequest) -> None:
    try:
        job.status = "running"
        job.push("start", {})
        params = ss.DetectParams(
            min_song=req.min_song,
            merge_gap=req.merge_gap,
            pad_start=req.pad_start,
            pad_end=req.pad_end,
            energy_floor_db=req.energy_floor_db,
            pulse_thresh=req.pulse_thresh,
        )

        def _progress(frac):
            job.push("tick", round(frac, 3))

        songs, _ = ss.detect_songs(req.wav, params, progress=_progress)
        songs_path = str(Path(req.out_dir) / "songs.json")
        ss.write_songs(songs, songs_path, meta={"wav": req.wav})
        result = {
            "songs_json": songs_path,
            "songs": [
                {"index": s.index, "start": s.start, "end": s.end, "label": s.label}
                for s in songs
            ],
        }
        job.result = result
        job.status = "done"
        job.push("done", result)
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.push("failed", str(e))


@router.post("/detect")
def start_detect(req: DetectRequest):
    Path(req.out_dir).mkdir(parents=True, exist_ok=True)
    job = _jobs.create("detect")
    threading.Thread(target=_run_detect, args=(job, req), daemon=True).start()
    return {"job_id": job.id}


# ── Waveform peaks ────────────────────────────────────────────────────────────

@router.get("/waveform")
def waveform(wav: str, width: int = 2000):
    """Downsampled waveform peaks for display. width = number of bars."""
    try:
        info = sf.info(wav)
        hop = max(1, info.frames // width)
        peaks: list[float] = []
        with sf.SoundFile(wav) as f:
            while True:
                chunk = f.read(hop, dtype="float32", always_2d=True)
                if len(chunk) == 0:
                    break
                mono = np.abs(chunk.mean(axis=1))
                peaks.append(float(mono.max()))
        m = max(peaks) if peaks else 1.0
        return {
            "peaks": [p / m for p in peaks],
            "duration": info.frames / info.samplerate,
            "samplerate": info.samplerate,
        }
    except Exception as e:
        raise HTTPException(400, str(e))


# ── Songs CRUD ────────────────────────────────────────────────────────────────

@router.get("/songs")
def read_songs(songs_json: str):
    songs = ss.read_songs(songs_json)
    return [
        {"index": s.index, "start": s.start, "end": s.end, "label": s.label}
        for s in songs
    ]


class SongEntry(BaseModel):
    index: int
    start: float
    end: float
    label: str


class SaveSongsRequest(BaseModel):
    songs_json: str
    songs: list[SongEntry]


@router.put("/songs")
def save_songs(req: SaveSongsRequest):
    songs = [ss.Song(s.index, s.start, s.end, s.label) for s in req.songs]
    ss.write_songs(songs, req.songs_json)
    return {"saved": len(songs)}
