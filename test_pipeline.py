#!/usr/bin/env python3
"""
End-to-end smoke test with synthetic media (no real footage needed).

Covers the full pipeline plus the three production fixes:
  - multi-clip sync map + song->clip routing (syncmap)
  - full-frame watermark auto-detection (watermark.resolve_mode)
  - hardware (VideoToolbox) AND software encoding paths
"""

import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

import media
import render as rnd
import song_splitter as ss
import syncmap
import watermark as wm

SR = 22050
TRUE_DELAY = 0.75  # camera starts 0.75 s before recorder -> offset L should be +0.75


def _build_master(path: Path):
    dur = 40.0
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = 0.01 * np.random.standard_normal(n)
    s0, s1 = int(14 * SR), int(28 * SR)
    y[s0:s1] += 0.25 * np.sin(2 * np.pi * 220 * t[s0:s1])
    for i in range(s0, s1, SR // 2):  # 120 BPM beats
        y[i:i + 400] += np.hanning(400) * 0.8
    y /= np.max(np.abs(y))
    sf.write(path, y, SR)
    return y


def _build_mov(master, mov_path, scratch_wav, w=640, h=360):
    d = int(TRUE_DELAY * SR)
    scratch = np.zeros_like(master)
    scratch[d:] = master[:len(master) - d]
    scratch += 0.04 * np.random.standard_normal(len(master))
    sf.write(scratch_wav, scratch * 0.5, SR)
    dur = len(master) / SR
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30:duration={dur}",
        "-i", str(scratch_wav),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(mov_path),
    ], check=True)


def _build_fullframe_watermark(path: Path, w=640, h=360):
    # a full-frame transparent canvas with a logo box in the corner
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black@0.0:size={w}x{h},format=rgba",
        "-f", "lavfi", "-i", "color=c=red@0.9:size=160x50,format=rgba",
        "-filter_complex", "[0][1]overlay=20:20",
        "-frames:v", "1", str(path),
    ], check=True)


def _check(name, cond, detail=""):
    print(f"[{name}] {'PASS' if cond else 'FAIL'} {detail}")
    return cond


def main() -> int:
    np.random.seed(1234)  # deterministic synthetic audio -> deterministic detection
    work = Path(tempfile.mkdtemp(prefix="idol_e2e_"))
    print(f"workdir: {work}")
    master_wav = work / "master.wav"
    mov = work / "show.mov"
    scratch = work / "scratch.wav"
    logo = work / "logo.png"
    scratch_dir = work / "scratchdir"

    master = _build_master(master_wav)
    _build_mov(master, mov, scratch)
    _build_fullframe_watermark(logo)
    ok = True

    # 0) watermark mode detection (unit)
    ok &= _check("wm-mode", wm.resolve_mode("auto", 640, 360, 640, 360) == "fullframe"
                 and wm.resolve_mode("auto", 1920, 1080, 200, 80) == "badge",
                 "fullframe vs badge")

    # 1) sync map (multi-clip path, here 1 clip)
    clips = syncmap.build_sync_map([str(mov)], str(master_wav), str(scratch_dir))
    c = clips[0]
    ok &= _check("sync", abs(c.offset - TRUE_DELAY) * 1000 < 30 and c.confidence >= 0.30,
                 f"L={c.offset:+.3f}s (true {TRUE_DELAY}) conf={c.confidence:.2f} "
                 f"cover={c.wav_start:.1f}..{c.wav_end:.1f}s")

    # 2) detect
    params = ss.DetectParams(min_song=8.0, merge_gap=6.0, pad_start=1.0, pad_end=1.0)
    songs, _ = ss.detect_songs(str(master_wav), params)
    ok &= _check("detect", len(songs) == 1 and 10 <= songs[0].start <= 16,
                 f"{len(songs)} song(s)" + (f" {songs[0].start:.1f}-{songs[0].end:.1f}s" if songs else ""))

    # 3) routing
    if songs:
        ok &= _check("route", syncmap.best_clip(clips, songs[0]) is c, "song -> covering clip")

    # 4) render — hardware + software, both with full-frame watermark + live progress
    for enc in ("hardware", "software"):
        out_dir = work / f"out_{enc}"
        opts = rnd.RenderOptions(out_dir=str(out_dir), encoder=enc, preset="ultrafast",
                                 watermark_png=str(logo), wm_mode="auto")
        ticks = []
        man = rnd.render_all(clips, str(master_wav), songs, opts,
                             on_song_tick=ticks.append)
        e = man[0]
        rendered = Path(e["output"]) if e["status"] == "ok" else None
        if rendered and rendered.exists():
            info = media.probe(rendered)
            ok &= _check(f"render/{enc}",
                         info.has_video and info.has_audio
                         and abs(info.duration - songs[0].duration) < 1.0
                         and info.v_codec == "h264",
                         f"{info.duration:.1f}s {info.v_codec} v={info.has_video} a={info.has_audio}")
            if enc == "hardware":
                hw_clip = rendered
        else:
            ok &= _check(f"render/{enc}", False, e.get("error", "no output"))
        ok &= _check(f"progress/{enc}",
                     len(ticks) >= 2 and ticks[-1] >= songs[0].duration * 0.8,
                     f"{len(ticks)} ticks, last={ticks[-1]:.1f}s" if ticks else "no ticks")

    # 5) combine — concat two song clips into a full performance
    full = work / "full_performance.mp4"
    opts = rnd.RenderOptions(out_dir=str(work))
    cticks = []
    rnd.combine_clips([str(hw_clip), str(hw_clip)], str(full), opts,
                      tick=cticks.append)
    if full.exists():
        fi = media.probe(full)
        ok &= _check("combine",
                     fi.has_video and fi.has_audio
                     and abs(fi.duration - 2 * songs[0].duration) < 1.5,
                     f"{fi.duration:.1f}s (≈2×{songs[0].duration:.0f}) ticks={len(cticks)}")
    else:
        ok &= _check("combine", False, "no full_performance output")

    print("\nRESULT:", "ALL PASS ✓" if ok else "FAILURES ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
