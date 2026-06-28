"""Render job endpoint — wraps the core render pipeline for the web app."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

import media
import render as rnd
import song_splitter as ss
import syncmap as sm
from app import jobs as _jobs

router = APIRouter()


class RenderRequest(BaseModel):
    sync_json: str
    songs_json: str
    out_dir: str
    prefix: str = ""
    mode: str = "songs"            # "songs" | "focus" | "overall"
    watermark: str | None = None
    fade: float = 1.0
    no_fade: bool = False
    fade_color: str = "black"
    encoder: str = "hardware"
    quality: int = 62
    endscreen: str | None = None
    endscreen_duration: float = 10.0
    full_start: str | None = None
    full_end: str | None = None


def _append_endscreen(full_path: Path, src: str, opts: rnd.RenderOptions,
                      duration: float, job: _jobs.Job) -> None:
    info = media.probe(str(full_path))
    w, h = info.width or 1920, info.height or 1080
    es_tmp = full_path.parent / "_es_tmp.mp4"
    full_tmp = full_path.parent / "_full_tmp.mp4"
    try:
        rnd.render_endscreen(
            src, opts, str(es_tmp), width=w, height=h, duration=duration,
            tick=lambda d: job.push("tick", d),
        )
        full_path.rename(full_tmp)
        rnd.combine_clips([str(full_tmp), str(es_tmp)], str(full_path), opts)
    finally:
        full_tmp.unlink(missing_ok=True)
        es_tmp.unlink(missing_ok=True)


def _run_render(job: _jobs.Job, req: RenderRequest) -> None:
    try:
        job.status = "running"
        clips, master_wav = sm.read_sync_map(req.sync_json)
        songs = ss.read_songs(req.songs_json)

        do_combine = req.mode == "focus"
        do_full = req.mode == "overall"
        eff_fade = 0.0 if req.no_fade else (1.5 if do_combine else req.fade)

        opts = rnd.RenderOptions(
            out_dir=req.out_dir,
            prefix=req.prefix,
            encoder=req.encoder,
            quality=req.quality,
            fade=eff_fade,
            fade_color=req.fade_color,
            watermark_png=req.watermark or None,
            wm_mode="auto",
        )

        # per-song render (render ALL songs so focus/overall full clips work)
        job.push("phase", "songs")
        manifest = rnd.render_all(
            clips, master_wav, songs, opts,
            on_song_begin=lambda s, c, total: job.push(
                "song_begin", {"label": s.label, "total": total}
            ),
            on_song_tick=lambda d: job.push("tick", d),
            on_progress=lambda e: job.push("song_done", e),
        )

        # focus mode — combine ALL songs into full_performance (regardless of render_solo)
        if do_combine:
            outputs = [m["output"] for m in manifest if m.get("status") == "ok"]
            if len(outputs) >= 2:
                job.push("phase", "full_performance")
                full_perf = Path(req.out_dir) / f"{req.prefix}full_performance.mp4"
                rnd.combine_clips(
                    outputs, str(full_perf), opts,
                    begin=lambda t: job.push("phase_begin", t),
                    tick=lambda d: job.push("tick", d),
                )
                if req.endscreen and full_perf.exists():
                    job.push("phase", "endscreen_performance")
                    _append_endscreen(full_perf, req.endscreen, opts,
                                      req.endscreen_duration, job)

        # remove individual clip files for songs the user didn't want as solo exports
        for entry, song in zip(manifest, songs):
            if not song.render_solo and entry.get("status") == "ok":
                Path(entry["output"]).unlink(missing_ok=True)
                entry["status"] = "skipped"

        # overall mode — full show (MC included)
        if do_full:
            job.push("phase", "full_show")
            start_t = ss.parse_time(req.full_start) if req.full_start else None
            end_t = ss.parse_time(req.full_end) if req.full_end else None
            full_show_out = rnd.render_full(
                clips, master_wav, opts,
                start=start_t, end=end_t, label="full_show",
                begin=lambda t: job.push("phase_begin", t),
                tick=lambda d: job.push("tick", d),
            )
            if req.endscreen and full_show_out.exists():
                job.push("phase", "endscreen_show")
                _append_endscreen(full_show_out, req.endscreen, opts,
                                  req.endscreen_duration, job)

        result = {"manifest": manifest, "out_dir": req.out_dir}
        job.result = result
        job.status = "done"
        job.push("done", result)

    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.push("failed", str(e))


@router.post("/render")
def start_render(req: RenderRequest):
    Path(req.out_dir).mkdir(parents=True, exist_ok=True)
    job = _jobs.create("render")
    threading.Thread(target=_run_render, args=(job, req), daemon=True).start()
    return {"job_id": job.id}
