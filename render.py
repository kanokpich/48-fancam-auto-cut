"""
Per-song render: pick the camera clip that covers the song, sync its video to the
master WAV audio, add an optional watermark, and encode.

Encoding defaults to Apple VideoToolbox hardware (h264_videotoolbox) — the Mac
Media Engine, the same path Final Cut Pro uses. It's ~4-8x faster than software
libx264. Use encoder="software" for archival-grade x264 (CRF) when you don't
care about speed.

Two output modes:
  deliverable (default) : H.264 + AAC, optional watermark, web-ready (faststart)
  lossless intermediate : -c:v copy, original codec, NO watermark, for grading
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import librosa

import audio_sync
import media
import watermark as wm
from syncmap import ClipSync, best_clip
from song_splitter import Song


@dataclass
class RenderOptions:
    out_dir: str
    prefix: str = ""
    codec: str = "h264"               # "h264" | "copy"
    encoder: str = "hardware"         # "hardware" (videotoolbox) | "software" (x264)
    quality: int = 62                 # videotoolbox -q:v (0..100, higher = better)
    crf: int = 18                     # software x264 CRF
    preset: str = "medium"            # software x264 preset
    audio_bitrate: str = "320k"
    container: str = "mp4"
    fade: float = 1.0                 # head/tail fade in/out seconds (0 = off)
    fade_color: str = "black"         # color to fade to/from (any ffmpeg color)
    hw_decode: bool = True            # videotoolbox decode when no filter is needed
    # watermark
    watermark_png: str | None = None
    wm_mode: str = "auto"             # "auto" | "badge" | "fullframe"
    wm_position: str = "br"
    wm_scale: float = 0.12
    wm_opacity: float = 0.85
    wm_width: int = 0                 # filled in by render_all (probed once)
    wm_height: int = 0
    # sync
    per_song_refine: bool = True


def _video_flags(opts: RenderOptions, copy_ok: bool) -> list[str]:
    if opts.codec == "copy" and copy_ok:
        return ["-c:v", "copy"]
    if opts.encoder == "hardware":
        return ["-c:v", "h264_videotoolbox", "-q:v", str(opts.quality),
                "-pix_fmt", "yuv420p", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-crf", str(opts.crf),
            "-preset", opts.preset, "-pix_fmt", "yuv420p"]


def render_song(clip: ClipSync, master_wav_path: str, song: Song, L: float,
                opts: RenderOptions, begin=None, tick=None) -> Path:
    """
    Render one song from one clip. Returns the output path. Raises on failure.

    begin(total_seconds) : called once the output duration is known.
    tick(done_seconds)   : called repeatedly with encode progress (live ffmpeg).
    """
    a, b = song.start, song.end
    # clamp the song to this clip's coverage (a song can spill past a clip edge)
    a = max(a, clip.wav_start)
    b = min(b, clip.wav_end)
    mov_start = a + L
    if mov_start < 0:
        a -= mov_start
        mov_start = 0.0
    duration = b - a
    if mov_start + duration > clip.duration:
        duration = max(0.0, clip.duration - mov_start)
    if duration <= 0.5:
        raise ValueError(f"{song.label}: no overlapping footage in {Path(clip.path).name}")

    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{opts.prefix}{song.label}" if opts.prefix else song.label
    out = out_dir / f"{stem}.{opts.container}"

    use_watermark = bool(opts.watermark_png) and opts.codec != "copy"
    # fade in/out (head/tail dissolve) — only when re-encoding, clamped to half the clip
    eff_fade = 0.0
    if opts.codec != "copy" and opts.fade > 0:
        eff_fade = min(opts.fade, max(0.0, duration / 2 - 0.05))
    use_filter = use_watermark or eff_fade > 0
    fade_out_st = max(0.0, duration - eff_fade)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    # hardware decode only when there's no CPU filtergraph (watermark/fade), else
    # the frames would need an extra download off the GPU.
    if opts.hw_decode and not use_filter and opts.codec != "copy":
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-ss", f"{mov_start:.3f}", "-i", clip.path,
            "-ss", f"{a:.3f}", "-i", master_wav_path]

    filter_parts: list[str] = []
    if use_watermark:
        cmd += ["-i", str(opts.watermark_png)]
        filter_parts.append(wm.overlay_filter(
            clip.width or 1920, clip.height or 1080,
            opts.wm_width, opts.wm_height, wm_input=2,
            mode=opts.wm_mode, position=opts.wm_position,
            scale=opts.wm_scale, opacity=opts.wm_opacity,
            out_label=("vw" if eff_fade > 0 else "v"),
        ))
        vlabel = "vw" if eff_fade > 0 else "v"
    else:
        vlabel = "0:v"

    if eff_fade > 0:
        f = f"{eff_fade:.3f}"
        st = f"{fade_out_st:.3f}"
        c = opts.fade_color
        filter_parts.append(
            f"[{vlabel}]fade=t=in:st=0:d={f}:color={c},fade=t=out:st={st}:d={f}:color={c}[v]")
        filter_parts.append(f"[1:a]afade=t=in:st=0:d={f},afade=t=out:st={st}:d={f}[a]")
        vmap, amap = "[v]", "[a]"
    else:
        vmap = "[v]" if use_watermark else "0:v"
        amap = "1:a"

    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", vmap, "-map", amap]

    cmd += ["-t", f"{duration:.3f}"]
    cmd += _video_flags(opts, copy_ok=not use_filter)
    cmd += ["-c:a", "aac", "-b:a", opts.audio_bitrate]
    if opts.container == "mp4":
        cmd += ["-movflags", "+faststart"]

    if begin:
        begin(duration)

    if tick is None:
        proc = subprocess.run(cmd + [str(out)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {song.label}:\n{proc.stderr.strip()}")
    else:
        _run_streaming(cmd + ["-progress", "pipe:1", "-nostats", str(out)],
                       duration, tick, song.label)
    return out


def _run_streaming(cmd: list[str], duration: float, tick, label: str) -> None:
    """Run ffmpeg, parsing `-progress` output to drive a live tick(done_seconds)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            v = line.split("=", 1)[1]
            if v.lstrip("-").isdigit():
                tick(min(duration, max(0.0, int(v) / 1_000_000)))
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"ffmpeg failed for {label}:\n{err.strip()}")
    tick(duration)


def render_all(clips: list[ClipSync], master_wav_path: str, songs: list[Song],
               opts: RenderOptions, sr: int = 22050, on_progress=None,
               on_song_begin=None, on_song_tick=None) -> list[dict]:
    """
    Render every song, routing each to the clip that covers it. Continues past
    per-song failures. Returns a manifest list of dicts.

    on_song_begin(song, clip, total_seconds) : a song's encode is starting.
    on_song_tick(done_seconds)               : live encode progress for that song.
    on_progress(entry)                       : a song finished (ok or failed).
    """
    # probe the watermark once
    if opts.watermark_png and opts.codec != "copy" and not opts.wm_width:
        info = media.probe(opts.watermark_png)
        opts.wm_width, opts.wm_height = info.width or 0, info.height or 0

    master = None
    scratch_cache: dict = {}
    if opts.per_song_refine:
        master, _ = librosa.load(master_wav_path, sr=sr, mono=True)

    manifest = []
    for song in songs:
        clip = best_clip(clips, song)
        entry = {"label": song.label, "start": song.start, "end": song.end,
                 "duration": round(song.duration, 2)}
        if clip is None:
            entry.update(status="failed", error="ไม่มีกล้องครอบคลุมช่วงเพลงนี้")
            manifest.append(entry)
            if on_progress:
                on_progress(entry)
            continue

        entry["clip"] = Path(clip.path).name
        L = clip.offset
        if opts.per_song_refine:
            L = _refine(clip, master, scratch_cache, song, sr) or clip.offset
        entry["offset"] = round(L, 3)

        begin = (lambda total, s=song, c=clip: on_song_begin(s, c, total)) if on_song_begin else None
        try:
            out = render_song(clip, master_wav_path, song, L, opts,
                              begin=begin, tick=on_song_tick)
            entry.update(status="ok", output=str(out))
        except (RuntimeError, ValueError) as e:
            entry.update(status="failed", error=str(e))
        manifest.append(entry)
        if on_progress:
            on_progress(entry)
    return manifest


def _refine(clip: ClipSync, master, scratch_cache: dict, song: Song, sr: int):
    """Per-song drift refinement against this clip's scratch audio (best effort)."""
    try:
        if clip.scratch_path not in scratch_cache:
            if not Path(clip.scratch_path).exists():
                media.extract_audio(clip.path, clip.scratch_path)
            scratch_cache[clip.scratch_path], _ = librosa.load(clip.scratch_path, sr=sr, mono=True)
        scratch = scratch_cache[clip.scratch_path]
        win = min(15.0, max(4.0, song.duration * 0.6))
        return audio_sync.refine_offset(scratch, master, sr, clip.offset,
                                        win=win, focus=(song.start, song.end))
    except Exception:
        return clip.offset


def combine_clips(clip_paths: list[str], out_path: str, opts: RenderOptions,
                  begin=None, tick=None) -> Path:
    """
    Concatenate rendered song clips into one 'full performance' file, in order.

    Each clip already carries its head/tail fade-to-color, so playing them back to
    back produces a dip-to-color transition between songs. If every clip shares the
    same dimensions/codec we stream-copy (instant); mixed resolutions (multi-camera)
    fall back to a scaled re-encode to the first clip's frame size.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    infos = [media.probe(p) for p in clip_paths]
    dims = {(i.width, i.height) for i in infos}
    total = sum(i.duration for i in infos)
    if begin:
        begin(total)

    if len(dims) == 1:
        # uniform -> concat demuxer, stream copy (no re-encode)
        listfile = out.parent / "_concat_list.txt"
        listfile.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in clip_paths))
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
               "-i", str(listfile), "-c", "copy", "-movflags", "+faststart"]
    else:
        # mixed resolutions -> scale each to the first clip's frame, re-encode
        w, h = infos[0].width or 1920, infos[0].height or 1080
        cmd = ["ffmpeg", "-y", "-v", "error"]
        for p in clip_paths:
            cmd += ["-i", p]
        parts, concat_in = [], ""
        for i in range(len(clip_paths)):
            parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
            concat_in += f"[v{i}][{i}:a]"
        fc = ";".join(parts) + f";{concat_in}concat=n={len(clip_paths)}:v=1:a=1[v][a]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        cmd += _video_flags(opts, copy_ok=False)
        cmd += ["-c:a", "aac", "-b:a", opts.audio_bitrate, "-movflags", "+faststart"]
        listfile = None

    if tick is None:
        proc = subprocess.run(cmd + [str(out)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"combine failed:\n{proc.stderr.strip()}")
    else:
        _run_streaming(cmd + ["-progress", "pipe:1", "-nostats", str(out)],
                       total, tick, "full_performance")
    if listfile:
        listfile.unlink(missing_ok=True)
    return out
