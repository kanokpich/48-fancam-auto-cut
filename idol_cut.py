#!/usr/bin/env python3
"""
idol-cut — dual-system pipeline for idol-event footage.

Stages
  1. sync    : align camera clips (.mov, one or many) to the external-recorder
               .wav (the WAV runs the whole show; cameras start/stop) -> sync.json
  2. detect  : find song boundaries on the clean WAV -> editable songs.json
  3. render  : cut one synced (+ watermarked) clip per song, routing each song to
               whichever camera clip covers it. Hardware-encoded (VideoToolbox).

Typical multi-camera run (files on an external SSD / SD card):

  python idol_cut.py volumes
  python idol_cut.py sync   --wav show.wav --mov camA1.mov --mov camA2.mov -o sync.json
  python idol_cut.py detect show.wav -o songs.json
  #   ... open songs.json, fix start/end, delete non-songs ...
  python idol_cut.py render --sync sync.json --songs songs.json --watermark logo.png -o output/show

Leave file flags off to pick interactively (macOS dialog; --no-gui for a menu).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import click
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                           TextColumn, TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table

import media
import pick
import render as rnd
import song_splitter as ss
import syncmap

console = Console()
SCRATCH = Path(tempfile.gettempdir()) / "idol_cut_scratch"

VIDEO_EXTS = ["mov", "mp4", "m4v", "avi", "mxf"]
AUDIO_EXTS = ["wav", "aif", "aiff", "flac"]


def _fmt(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


def _progress(elapsed: bool = False) -> Progress:
    cols = [SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn()]
    cols.append(TimeElapsedColumn() if elapsed else TimeRemainingColumn())
    return Progress(*cols, console=console)


# ── interactive input resolution ─────────────────────────────────────────────

def _pick_required(prompt, exts, gui, location=None):
    try:
        p = pick.choose_file(prompt, exts, gui=gui, location=location)
    except pick.PickCancelled:
        console.print("[red]ยกเลิก[/red]")
        raise click.Abort()
    console.print(f"  → [cyan]{p}[/cyan]")
    return str(p)


def _resolve_videos(mov_paths, gui):
    if mov_paths:
        return [str(m) for m in mov_paths]
    try:
        files = pick.choose_files("เลือกไฟล์วิดีโอ (เลือกได้หลายไฟล์)", VIDEO_EXTS, gui=gui)
    except pick.PickCancelled:
        console.print("[red]ยกเลิก[/red]")
        raise click.Abort()
    for f in files:
        console.print(f"  → [cyan]{f}[/cyan]")
    return [str(f) for f in files]


def _resolve_watermark(watermark_png, no_watermark, lossless, gui):
    if watermark_png or no_watermark or lossless:
        return watermark_png
    wm_path = pick.choose_file("เลือกลายน้ำ (.png) — Cancel = ไม่ใส่", ["png"],
                               allow_skip=True, gui=gui)
    console.print(f"  → ลายน้ำ: [cyan]{wm_path or 'ไม่ใส่'}[/cyan]")
    return str(wm_path) if wm_path else None


def _resolve_out_dir(out_dir, master_wav, gui):
    if out_dir:
        return out_dir
    default = str(Path("output") / Path(master_wav).stem)
    try:
        p = pick.choose_folder("เลือกโฟลเดอร์ปลายทางสำหรับไฟล์ที่ render", gui=gui)
    except pick.PickCancelled:
        console.print(f"  → output (default): [cyan]{default}[/cyan]")
        return default
    console.print(f"  → output: [cyan]{p}[/cyan]")
    return str(p)


def _clip_line(c: syncmap.ClipSync) -> str:
    col = "green" if c.confidence >= 0.30 else "yellow"
    return f"  [{col}]✓[/] {Path(c.path).name}  L={c.offset:+.3f}s  conf={c.confidence:.2f}"


def _sync_with_progress(mov_paths, wav_path, no_refine):
    """build_sync_map wrapped in a per-clip progress bar."""
    with _progress(elapsed=True) as prog:
        task = prog.add_task("Syncing clips", total=len(mov_paths))

        def cp(clip):
            prog.advance(task, 1)
            prog.console.print(_clip_line(clip))

        return syncmap.build_sync_map(mov_paths, wav_path, str(SCRATCH),
                                      refine=not no_refine, on_progress=cp)


def _print_clips(clips: list[syncmap.ClipSync]):
    table = Table(title=f"Sync map ({len(clips)} clip(s))")
    table.add_column("Clip")
    table.add_column("Offset", justify="right")
    table.add_column("WAV coverage", justify="right")
    table.add_column("Conf", justify="right")
    for c in clips:
        col = "green" if c.confidence >= 0.30 else "yellow"
        table.add_row(Path(c.path).name, f"{c.offset:+.3f}s",
                      f"{_fmt(max(0, c.wav_start))}–{_fmt(c.wav_end)}",
                      f"[{col}]{c.confidence:.2f}[/{col}]")
    console.print(table)


def _print_songs(songs: list[ss.Song]):
    table = Table(title=f"Songs ({len(songs)})")
    table.add_column("#")
    table.add_column("Label")
    table.add_column("Start", justify="right")
    table.add_column("End", justify="right")
    table.add_column("Duration", justify="right")
    for s in songs:
        table.add_row(str(s.index), s.label, _fmt(s.start), _fmt(s.end), _fmt(s.duration))
    console.print(table)


# ── volumes ──────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """idol-cut — sync, split-by-song, and watermark idol-event footage."""


@cli.command()
def volumes():
    """List mounted drives (find your SD card / SSD)."""
    vols = media.list_volumes()
    if not vols:
        console.print("[red]ไม่มีไดรฟ์ที่ mount อยู่[/red]")
        return
    table = Table(title="Mounted volumes (/Volumes)")
    table.add_column("Name")
    table.add_column("Path", style="dim")
    for v in vols:
        table.add_row(v.name, str(v))
    console.print(table)


# ── sync ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--mov", "mov_paths", multiple=True, help="วิดีโอ (ใส่ได้หลายไฟล์; ไม่ใส่ = เลือกตอนรัน)")
@click.option("--wav", "wav_path", default=None, help="External recorder .wav (master)")
@click.option("-o", "--out", "out_path", default="sync.json", show_default=True, help="Sync map output")
@click.option("--no-refine", is_flag=True, help="Skip sample-accurate refinement")
@click.option("--no-gui", is_flag=True, help="ใช้เมนูใน terminal แทน dialog ของ Mac")
def sync(mov_paths, wav_path, out_path, no_refine, no_gui):
    """Sync one or many camera clips to the master WAV -> sync.json."""
    gui = not no_gui
    mov_paths = _resolve_videos(mov_paths, gui)
    if not wav_path:
        wav_path = _pick_required("เลือกไฟล์เสียง (.wav)", AUDIO_EXTS, gui)

    SCRATCH.mkdir(parents=True, exist_ok=True)
    console.print(f"[cyan]Syncing {len(mov_paths)} clip(s) to {Path(wav_path).name}…[/cyan]")
    clips = _sync_with_progress(mov_paths, wav_path, no_refine)
    syncmap.write_sync_map(clips, out_path, wav_path)
    _print_clips(clips)
    weak = [c for c in clips if c.confidence < 0.30]
    if weak:
        console.print(f"[yellow]⚠ {len(weak)} clip มี confidence ต่ำ — เช็คว่าเสียงกล้องพอได้ยินมั้ย[/yellow]")
    console.print(f"[green]✓ เขียน {out_path}[/green]")


# ── detect ───────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("wav_path", required=False)
@click.option("-o", "--out", default="songs.json", show_default=True, help="Editable cut list")
@click.option("--min-song", default=60.0, show_default=True, help="Discard music runs shorter than this (s)")
@click.option("--merge-gap", default=12.0, show_default=True, help="Bridge internal gaps shorter than this (s)")
@click.option("--pulse-thresh", default=0.30, show_default=True, help="Beat-clarity threshold (0..1)")
@click.option("--energy-floor", default=25.0, show_default=True, help="dB below loudest to still count as music")
@click.option("--features-csv", default=None, help="Dump per-window features for tuning")
@click.option("--no-gui", is_flag=True, help="ใช้เมนูใน terminal แทน dialog ของ Mac")
def detect(wav_path, out, min_song, merge_gap, pulse_thresh, energy_floor, features_csv, no_gui):
    """Detect song boundaries on the master WAV -> editable songs.json."""
    if not wav_path:
        wav_path = _pick_required("เลือกไฟล์เสียง (.wav)", AUDIO_EXTS, gui=not no_gui)
    wav = media.probe(wav_path)
    console.print(f"[cyan]Analyzing[/cyan] {wav.path.name} ({_fmt(wav.duration)})…")

    params = ss.DetectParams(min_song=min_song, merge_gap=merge_gap,
                             pulse_thresh=pulse_thresh, energy_floor_db=energy_floor)
    with _progress() as prog:
        task = prog.add_task("Analyzing audio", total=1.0)
        songs, rows = ss.detect_songs(str(wav.path), params,
                                      progress=lambda f: prog.update(task, completed=f))

    if features_csv:
        import numpy as np
        np.savetxt(features_csv, rows, delimiter=",",
                   header="time,energy_db,pulse,is_music", comments="")
        console.print(f"[dim]features -> {features_csv}[/dim]")

    if not songs:
        console.print("[yellow]ไม่เจอเพลง — ลองลด --pulse-thresh หรือ --min-song[/yellow]")
        return

    _print_songs(songs)
    ss.write_songs(songs, out, meta={"wav": str(wav.path), "duration": wav.duration})
    console.print(f"[green]✓ เขียน {out} แล้ว[/green] — เปิดแก้ start/end ก่อน render ได้เลย")


# ── render ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--sync", "sync_path", default=None, help="sync.json จากคำสั่ง sync (งานหลายกล้อง)")
@click.option("--mov", "mov_paths", multiple=True, help="วิดีโอ (หลายไฟล์ได้; ไม่ใส่และไม่มี --sync = เลือกตอนรัน)")
@click.option("--wav", "wav_path", default=None, help="External recorder .wav (ถ้าไม่ใช้ --sync)")
@click.option("--songs", "songs_path", default=None, help="songs.json (ไม่ใส่ = เลือกตอนรัน)")
@click.option("-o", "--out", "out_dir", default=None, help="Output dir (ไม่ใส่ = output/<ชื่อ wav>)")
@click.option("--prefix", default="", help="Filename prefix, e.g. 2026-06-27_")
@click.option("--watermark", "watermark_png", default=None, help="PNG watermark (ไม่ใส่ = เลือกตอนรัน, Cancel = ไม่ใส่)")
@click.option("--no-watermark", is_flag=True, help="ไม่ใส่ลายน้ำ (ไม่ต้องถาม)")
@click.option("--wm-mode", type=click.Choice(["auto", "badge", "fullframe"]), default="auto",
              show_default=True, help="auto=เดาเอง, fullframe=ลายน้ำเต็มจอ, badge=โลโก้มุมจอ")
@click.option("--wm-position", type=click.Choice(["tl", "tr", "bl", "br", "center"]),
              default="br", show_default=True)
@click.option("--wm-scale", default=0.12, show_default=True, help="(badge) ความกว้างลายน้ำเทียบวิดีโอ")
@click.option("--wm-opacity", default=0.85, show_default=True)
@click.option("--encoder", type=click.Choice(["hardware", "software"]), default="hardware",
              show_default=True, help="hardware=videotoolbox (เร็ว เหมือน FCPX), software=x264 (ช้า archival)")
@click.option("--quality", default=62, show_default=True, help="videotoolbox q:v 0-100 (สูง=คุณภาพดี)")
@click.option("--fade", default=None, type=float, help="fade หัว-ท้ายคลิป วินาที (default 1.0, หรือ 1.5 ถ้า --combine)")
@click.option("--no-fade", is_flag=True, help="ไม่ใส่ fade")
@click.option("--fade-color", default="black", show_default=True, help="สีที่ fade ไปหา (black/white/0xRRGGBB)")
@click.option("--combine", is_flag=True, help="รวมทุกเพลงต่อกันเป็น full_performance.mp4 (dip-to-color ระหว่างเพลง)")
@click.option("--lossless", is_flag=True, help="Stream-copy video (ไม่ encode, ไม่ลายน้ำ) สำหรับเอาไป grade")
@click.option("--no-refine", is_flag=True, help="ใช้ offset เดียว (ข้าม per-song drift correction)")
@click.option("--cache-local", is_flag=True, help="Copy sources off the card to local scratch first")
@click.option("--no-gui", is_flag=True, help="ใช้เมนูใน terminal แทน dialog ของ Mac")
def render(sync_path, mov_paths, wav_path, songs_path, out_dir, prefix, watermark_png,
           no_watermark, wm_mode, wm_position, wm_scale, wm_opacity, encoder, quality,
           fade, no_fade, fade_color, combine, lossless, no_refine, cache_local, no_gui):
    """Cut one synced (+ watermarked) clip per song, routing songs to covering clips."""
    gui = not no_gui
    SCRATCH.mkdir(parents=True, exist_ok=True)

    # 1) clips + master wav (from a saved sync map, or build one now)
    if sync_path:
        clips, master_wav = syncmap.read_sync_map(sync_path)
        if not master_wav:
            console.print("[red]sync.json ไม่มี master_wav[/red]")
            raise click.Abort()
        console.print(f"[cyan]{len(clips)} clip(s)[/cyan] from {sync_path}")
    else:
        mov_paths = _resolve_videos(mov_paths, gui)
        if not wav_path:
            wav_path = _pick_required("เลือกไฟล์เสียง (.wav)", AUDIO_EXTS, gui)
        if cache_local:
            console.print("[cyan]Caching sources to local scratch…[/cyan]")
            mov_paths = [str(media.cache_local(m, SCRATCH)) for m in mov_paths]
            wav_path = str(media.cache_local(wav_path, SCRATCH))
        console.print(f"[cyan]Syncing {len(mov_paths)} clip(s)…[/cyan]")
        clips = _sync_with_progress(mov_paths, wav_path, no_refine)
        master_wav = wav_path

    _print_clips(clips)

    # 2) songs
    if not songs_path:
        songs_path = _pick_required("เลือก songs.json", ["json"], gui, location=Path.cwd())
    songs = ss.read_songs(songs_path)
    console.print(f"[cyan]{len(songs)} songs[/cyan] from {songs_path}")

    # 3) watermark + output dir (output dir is chosen at runtime if not given)
    watermark_png = _resolve_watermark(watermark_png, no_watermark, lossless, gui)
    if lossless and watermark_png:
        console.print("[yellow]⚠ --lossless ใส่ลายน้ำไม่ได้ (ต้อง re-encode) — ข้ามลายน้ำ[/yellow]")
        watermark_png = None
    out_dir = _resolve_out_dir(out_dir, master_wav, gui)

    eff_fade = 0.0 if no_fade else (fade if fade is not None else (1.5 if combine else 1.0))
    opts = rnd.RenderOptions(
        out_dir=out_dir, prefix=prefix,
        codec="copy" if lossless else "h264",
        encoder=encoder, quality=quality,
        fade=eff_fade, fade_color=fade_color,
        watermark_png=watermark_png, wm_mode=wm_mode, wm_position=wm_position,
        wm_scale=wm_scale, wm_opacity=wm_opacity,
        per_song_refine=not no_refine,
    )

    console.print(f"[cyan]Rendering ({encoder}) → {out_dir}[/cyan]")
    cur = {"total": 1.0}
    with _progress() as prog:
        overall = prog.add_task("[bold]Overall", total=len(songs))
        song_bar = prog.add_task("", total=1.0, visible=False)

        def on_begin(song, clip, total):
            cur["total"] = total or 1.0
            prog.update(song_bar, total=cur["total"], completed=0.0, visible=True,
                        description=f"  {song.label} [dim]{Path(clip.path).name}[/dim]")

        def on_tick(done):
            prog.update(song_bar, completed=done)

        def on_done(e):
            prog.update(song_bar, completed=cur["total"])
            prog.advance(overall, 1)
            mark, col = ("✓", "green") if e["status"] == "ok" else ("✗", "red")
            line = (f"  [{col}]{mark} {e['label']}[/] {_fmt(e['duration'])}"
                    + (f" [dim]({e.get('clip','')})[/dim]" if e.get('clip') else "")
                    + (f"  [red]{e.get('error','')}[/red]" if e["status"] != "ok" else ""))
            prog.console.print(line)

        manifest = rnd.render_all(
            clips, master_wav, songs, opts,
            on_progress=on_done, on_song_begin=on_begin, on_song_tick=on_tick,
        )
        prog.update(song_bar, visible=False)

    manifest_path = Path(out_dir) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    ok = sum(1 for m in manifest if m["status"] == "ok")
    console.print(f"[bold green]Done: {ok}/{len(manifest)} songs[/bold green] → {out_dir}")
    if ok < len(manifest):
        console.print("[yellow]บางเพลง fail — ดู manifest.json[/yellow]")

    # combined "full performance" video
    if combine:
        outputs = [m["output"] for m in manifest if m["status"] == "ok"]
        if len(outputs) < 2:
            console.print("[yellow]⚠ --combine ต้องมีอย่างน้อย 2 เพลงที่ render สำเร็จ — ข้าม[/yellow]")
        else:
            full = Path(out_dir) / f"{prefix}full_performance.{opts.container}"
            console.print(f"[cyan]รวม {len(outputs)} เพลง → {full.name}[/cyan]")
            with _progress() as prog:
                task = prog.add_task("  full_performance", total=1.0)
                try:
                    rnd.combine_clips(
                        outputs, str(full), opts,
                        begin=lambda total: prog.update(task, total=total, completed=0.0),
                        tick=lambda d: prog.update(task, completed=d))
                    console.print(f"[bold green]✓ full performance[/bold green] → {full}")
                except RuntimeError as e:
                    console.print(f"[red]✗ combine fail: {e}[/red]")


# ── auto ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--mov", "mov_paths", multiple=True, help="วิดีโอ (หลายไฟล์ได้; ไม่ใส่ = เลือกตอนรัน)")
@click.option("--wav", "wav_path", default=None, help="External recorder .wav")
@click.option("-o", "--out", "out_dir", default=None, help="Output dir (ไม่ใส่ = output/<ชื่อ wav>)")
@click.option("--watermark", "watermark_png", default=None, help="PNG watermark (ไม่ใส่ = เลือกตอนรัน)")
@click.option("--no-watermark", is_flag=True, help="ไม่ใส่ลายน้ำ (ไม่ต้องถาม)")
@click.option("--combine", is_flag=True, help="รวมทุกเพลงเป็น full_performance.mp4 ด้วย")
@click.option("--prefix", default="")
@click.option("--yes", is_flag=True, help="Skip the review pause and render immediately")
@click.option("--no-gui", is_flag=True, help="ใช้เมนูใน terminal แทน dialog ของ Mac")
@click.pass_context
def auto(ctx, mov_paths, wav_path, out_dir, watermark_png, no_watermark, combine, prefix, yes, no_gui):
    """sync → detect → (review) → render, in one command."""
    gui = not no_gui
    mov_paths = _resolve_videos(mov_paths, gui)
    if not wav_path:
        wav_path = _pick_required("เลือกไฟล์เสียง (.wav)", AUDIO_EXTS, gui)
    watermark_png = _resolve_watermark(watermark_png, no_watermark, False, gui)
    out_dir = _resolve_out_dir(out_dir, wav_path, gui)

    wav = media.probe(wav_path)
    songs_path = str(Path(out_dir) / "songs.json")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    songs, _ = ss.detect_songs(str(wav.path))
    if not songs:
        console.print("[yellow]ไม่เจอเพลง — ลอง detect แยกแล้วปรับ threshold[/yellow]")
        return
    _print_songs(songs)
    ss.write_songs(songs, songs_path, meta={"wav": str(wav.path)})
    console.print(f"[green]✓ {songs_path}[/green]")

    if not yes:
        mov_args = " ".join(f"--mov '{m}'" for m in mov_paths)
        console.print(
            f"\n[bold]รีวิว/แก้ {songs_path} ก่อน[/bold] แล้วรัน:\n"
            f"  python idol_cut.py render {mov_args} --wav '{wav_path}' "
            f"--songs '{songs_path}' -o '{out_dir}'"
            + (f" --watermark '{watermark_png}'" if watermark_png else "")
        )
        return

    ctx.invoke(render, sync_path=None, mov_paths=tuple(mov_paths), wav_path=wav_path,
               songs_path=songs_path, out_dir=out_dir, prefix=prefix,
               watermark_png=watermark_png, no_watermark=no_watermark,
               wm_mode="auto", wm_position="br", wm_scale=0.12, wm_opacity=0.85,
               encoder="hardware", quality=62, fade=None, no_fade=False,
               fade_color="black", combine=combine,
               lossless=False, no_refine=False, cache_local=False, no_gui=no_gui)


if __name__ == "__main__":
    cli()
