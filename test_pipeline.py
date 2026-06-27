#!/usr/bin/env python3
"""
End-to-end smoke test with synthetic media (no real footage needed).

Covers the full pipeline plus the three production fixes:
  - multi-clip sync map + song->clip routing (syncmap)
  - full-frame watermark auto-detection (watermark.resolve_mode)
  - hardware (VideoToolbox) AND software encoding paths

Phase 2 additions:
  - render_full: full_show includes MC gap (overall mode)
  - render_full multi-file: two contiguous clips stitch into one
  - render_full trim: --full-start/--full-end
  - fade_sides: no color dip at internal stitch seams
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


def _build_master(path: Path, dur=40.0):
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


def _build_master_two_songs(path: Path, dur=46.0):
    """master with song1 [4,14], quiet MC [14,28], song2 [28,40], then silence."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    y = 0.01 * np.random.standard_normal(n)
    for s0, s1 in [(4, 14), (28, 40)]:
        a, b = int(s0 * SR), int(s1 * SR)
        y[a:b] += 0.25 * np.sin(2 * np.pi * 220 * t[a:b])
        for i in range(a, b, SR // 2):
            y[i:i + 400] += np.hanning(400) * 0.8
    y /= np.max(np.abs(y))
    sf.write(path, y, SR)
    return y


def _build_mov(master, mov_path, scratch_wav, w=640, h=360, delay=TRUE_DELAY):
    d = int(delay * SR)
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
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black@0.0:size={w}x{h},format=rgba",
        "-f", "lavfi", "-i", "color=c=red@0.9:size=160x50,format=rgba",
        "-filter_complex", "[0][1]overlay=20:20",
        "-frames:v", "1", str(path),
    ], check=True)


def _luma_at(video_path: str, seek: float, window: float = 0.5) -> float:
    """Average luma (Y channel) over a short window, via rawvideo pipe."""
    proc = subprocess.run([
        "ffmpeg", "-v", "error",
        "-ss", str(seek), "-t", str(window),
        "-i", video_path,
        "-vf", "scale=32:18,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ], capture_output=True)
    data = proc.stdout
    if not data:
        return 128.0
    return float(np.frombuffer(data, np.uint8).mean())


def _check(name, cond, detail=""):
    print(f"[{name}] {'PASS' if cond else 'FAIL'} {detail}")
    return cond


def main() -> int:
    np.random.seed(1234)
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
    hw_clip = None
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
    opts_base = rnd.RenderOptions(out_dir=str(work))
    cticks = []
    rnd.combine_clips([str(hw_clip), str(hw_clip)], str(full), opts_base,
                      tick=cticks.append)
    if full.exists():
        fi = media.probe(full)
        ok &= _check("combine",
                     fi.has_video and fi.has_audio
                     and abs(fi.duration - 2 * songs[0].duration) < 1.5,
                     f"{fi.duration:.1f}s (≈2×{songs[0].duration:.0f}) ticks={len(cticks)}")
    else:
        ok &= _check("combine", False, "no full_performance output")

    # ── Phase 2 tests ────────────────────────────────────────────────────────
    print()
    print("=== Phase 2: overall / focus / full ===")
    np.random.seed(5678)

    # Build a show with two songs + MC gap
    #   song1: WAV [4,14]   quiet MC: [14,28]   song2: [28,40]   total: 46s
    p2_wav = work / "p2_master.wav"
    p2_mov = work / "p2_show.mov"
    p2_sc = work / "p2_scratch.wav"
    p2_master = _build_master_two_songs(p2_wav, dur=46.0)
    _build_mov(p2_master, p2_mov, p2_sc, delay=TRUE_DELAY)
    p2_clips = syncmap.build_sync_map([str(p2_mov)], str(p2_wav), str(scratch_dir / "p2"))
    p2_songs = [ss.Song(1, 4.0, 14.0, "song01"), ss.Song(2, 28.0, 40.0, "song02")]

    # 6) focus combine: full_performance ≈ song1+song2 duration (MC excluded)
    focus_dir = work / "focus"
    focus_opts = rnd.RenderOptions(out_dir=str(focus_dir), encoder="software",
                                   preset="ultrafast", fade=1.5)
    focus_man = rnd.render_all(p2_clips, str(p2_wav), p2_songs, focus_opts)
    focus_outs = [m["output"] for m in focus_man if m["status"] == "ok"]
    fp_path = focus_dir / "full_performance.mp4"
    rnd.combine_clips(focus_outs, str(fp_path), focus_opts)
    if fp_path.exists():
        fi = media.probe(fp_path)
        song_dur = sum(s.duration for s in p2_songs)
        ok &= _check("focus/combine",
                     abs(fi.duration - song_dur) < 3.0,
                     f"{fi.duration:.1f}s (songs only ≈{song_dur:.0f}s, MC excluded)")
    else:
        ok &= _check("focus/combine", False, "no full_performance")

    # 7) overall full_show: full_show ≈ whole take (MC included) > combine
    overall_dir = work / "overall"
    overall_opts = rnd.RenderOptions(out_dir=str(overall_dir), encoder="software",
                                     preset="ultrafast", fade=1.0)
    fs_path = rnd.render_full(p2_clips, str(p2_wav), overall_opts, label="full_show")
    if fs_path.exists():
        fi_fs = media.probe(fs_path)
        take_dur = p2_clips[0].wav_end - max(0.0, p2_clips[0].wav_start)
        ok &= _check("overall/full",
                     fi_fs.duration >= take_dur * 0.9,
                     f"{fi_fs.duration:.1f}s (take≈{take_dur:.0f}s, MC included)")
        if fp_path.exists():
            ok &= _check("overall/full>combine",
                         fi_fs.duration > media.probe(fp_path).duration + 5.0,
                         "full_show longer than full_performance (MC gap counted)")
    else:
        ok &= _check("overall/full", False, "no full_show")

    # 8) multi-file full: two synthetic ClipSync objects covering different WAV spans
    #    → tests the multi-segment stitch code path without relying on sync accuracy
    #    cam1: WAV [0,23s]  cam2: WAV [22,44s]  → together cover [0,44s] ≈ 44s
    cam1_cs = syncmap.ClipSync(
        path=str(p2_mov), offset=0.0, duration=23.0, confidence=0.9,
        width=640, height=360, scratch_path="", label="cam1",
    )
    cam2_cs = syncmap.ClipSync(
        path=str(p2_mov), offset=-22.0, duration=22.0, confidence=0.9,
        width=640, height=360, scratch_path="", label="cam2",
    )
    multi_clips = [cam1_cs, cam2_cs]  # already sorted by wav_start (0, 22)
    multi_dir = work / "multi"
    multi_opts = rnd.RenderOptions(out_dir=str(multi_dir), encoder="software",
                                   preset="ultrafast", fade=1.0, per_song_refine=False)
    mf_path = rnd.render_full(multi_clips, str(p2_wav), multi_opts, label="full_show")
    fi_mf = None
    if mf_path.exists():
        fi_mf = media.probe(mf_path)
        ok &= _check("multi/full",
                     fi_mf.duration >= 30.0,
                     f"{fi_mf.duration:.1f}s (two synthetic clips, ≥30s)")
    else:
        ok &= _check("multi/full", False, "no multi full_show")

    # 9) trim: --full-start/--full-end shrinks the output
    trim_dir = work / "trim"
    trim_opts = rnd.RenderOptions(out_dir=str(trim_dir), encoder="software",
                                  preset="ultrafast", fade=1.0)
    trim_start, trim_end = 5.0, 35.0
    tr_path = rnd.render_full(p2_clips, str(p2_wav), trim_opts,
                               start=trim_start, end=trim_end, label="full_show")
    if tr_path.exists():
        fi_tr = media.probe(tr_path)
        expected = trim_end - trim_start
        ok &= _check("trim/full",
                     abs(fi_tr.duration - expected) < 2.0,
                     f"{fi_tr.duration:.1f}s (expected ≈{expected:.0f}s)")
    else:
        ok &= _check("trim/full", False, "no trimmed full_show")

    # 10) fade_sides: head is dark (fade-in), seam is bright (no dip at internal stitch)
    if fi_mf and fi_mf.duration > 20.0:
        # head: first 0.5s should be fading in from black → dark
        luma_head = _luma_at(str(mf_path), seek=0.1, window=0.4)
        # seam: middle of the stitched output → no fade applied → bright content
        mid = fi_mf.duration / 2
        luma_mid = _luma_at(str(mf_path), seek=mid - 0.3, window=0.6)
        ok &= _check("fade_sides/head",
                     luma_head < 50,
                     f"head luma={luma_head:.1f} (expect dark — fading in from black)")
        ok &= _check("fade_sides/seam",
                     luma_mid > 60,
                     f"seam luma={luma_mid:.1f} (expect bright — no fade at internal seam)")
    else:
        ok &= _check("fade_sides", False, "skip: no multi full_show to probe")

    # ── endscreen tests ──────────────────────────────────────────────────────
    print()
    print("=== Endscreen ===")

    es_dir = work / "endscreen"
    es_opts = rnd.RenderOptions(out_dir=str(es_dir), encoder="software",
                                preset="ultrafast", fade=1.0)

    # 11) image endscreen: PNG → 5s video, fades in, correct duration
    es_clip_path = es_dir / "es.mp4"
    es_clip_path.parent.mkdir(parents=True, exist_ok=True)
    es_ticks = []
    rnd.render_endscreen(
        str(logo), es_opts, str(es_clip_path),
        width=640, height=360, duration=5.0,
        tick=es_ticks.append,
    )
    if es_clip_path.exists():
        fi_es = media.probe(es_clip_path)
        ok &= _check("endscreen/image",
                     fi_es.has_video and abs(fi_es.duration - 5.0) < 0.5,
                     f"{fi_es.duration:.1f}s h264={fi_es.v_codec}")
        luma_es_head = _luma_at(str(es_clip_path), seek=0.1, window=0.4)
        ok &= _check("endscreen/fade-in",
                     luma_es_head < 50,
                     f"head luma={luma_es_head:.1f} (expect dark — fading from black)")
    else:
        ok &= _check("endscreen/image", False, "no output")

    # 12) endscreen appended to full_performance (focus combine)
    if fp_path.exists():
        dur_before = media.probe(fp_path).duration
        import shutil as _sh
        fp2 = focus_dir / "fp_with_es.mp4"
        _sh.copy2(fp_path, fp2)  # work on a copy to preserve original for other checks
        rnd.combine_clips([str(fp2), str(es_clip_path)], str(fp2.parent / "fp_final.mp4"), focus_opts)
        fi_fp_final = media.probe(str(fp2.parent / "fp_final.mp4"))
        ok &= _check("endscreen/focus-append",
                     abs(fi_fp_final.duration - (dur_before + fi_es.duration)) < 1.5,
                     f"{fi_fp_final.duration:.1f}s "
                     f"(combine {dur_before:.0f}s + endscreen {fi_es.duration:.0f}s)")
    else:
        ok &= _check("endscreen/focus-append", False, "no full_performance to append to")

    print()
    print("RESULT:", "ALL PASS ✓" if ok else "FAILURES ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
