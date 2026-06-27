#!/usr/bin/env python3
"""
auto-cut: Audio-based automatic video cutter for idol events.

Modes:
  beat    — cut on every N beats (great for music-synced edits)
  onset   — cut around loud events: applause, drops, chants
  silence — cut at natural silence gaps
  highlight — pick top-N highest-energy clips from the whole video

Usage:
  python auto_cut.py beat    input.mp4 output.mp4 --every 2
  python auto_cut.py onset   input.mp4 output.mp4 --clip-length 4
  python auto_cut.py silence input.mp4 output.mp4
  python auto_cut.py highlight input.mp4 output.mp4 --top 10
"""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

import beat_detector as bd
import cut_generator as cg
import exporter as ex

console = Console()


def _load_and_analyze(input_path: str):
    console.print(f"[cyan]Loading audio from[/cyan] {input_path} …")
    y, sr = bd.extract_audio(input_path)
    duration = len(y) / sr
    console.print(f"[green]✓[/green] Duration: {duration:.1f}s  |  Sample rate: {sr}Hz")
    return y, sr, duration


def _preview_segments(segments: list[tuple[float, float]], label: str) -> None:
    table = Table(title=f"{label} ({len(segments)} cuts)")
    table.add_column("#", style="dim")
    table.add_column("Start", justify="right")
    table.add_column("End", justify="right")
    table.add_column("Duration", justify="right")
    for i, (s, e) in enumerate(segments, 1):
        table.add_row(str(i), f"{s:.2f}s", f"{e:.2f}s", f"{e-s:.2f}s")
    console.print(table)


def _do_export(segments, input_path, output, edl, json_out, reencode, fps):
    out = Path(output)

    if edl:
        edl_path = out.with_suffix(".edl")
        ex.export_edl(segments, str(edl_path), source_name=Path(input_path).stem, fps=fps)
        console.print(f"[green]✓ EDL saved:[/green] {edl_path}")

    if json_out:
        json_path = out.with_suffix(".json")
        ex.export_json(segments, str(json_path))
        console.print(f"[green]✓ JSON saved:[/green] {json_path}")

    console.print(f"[cyan]Rendering[/cyan] {len(segments)} segments → {out} …")
    ex.export_ffmpeg(input_path, segments, str(out), reencode=reencode)
    console.print(f"[bold green]✓ Done:[/bold green] {out}")


# ── Shared options ─────────────────────────────────────────────────────────────
_shared = [
    click.argument("input_path"),
    click.argument("output"),
    click.option("--edl", is_flag=True, help="Also export a CMX3600 EDL for DaVinci/Premiere"),
    click.option("--json", "json_out", is_flag=True, help="Also export cut list as JSON"),
    click.option("--reencode", is_flag=True, help="Re-encode (slower but frame-accurate cuts)"),
    click.option("--fps", default=29.97, show_default=True, help="FPS for EDL timecode"),
]

def shared_options(f):
    for opt in reversed(_shared):
        f = opt(f)
    return f


@click.group()
def cli():
    """auto-cut — audio-based video cutter for idol events."""


@cli.command()
@shared_options
@click.option("--every", default=2, show_default=True, help="Cut every N beats (1=each beat, 4=each bar)")
@click.option("--min-length", default=1.0, show_default=True, help="Minimum clip length in seconds")
def beat(input_path, output, edl, json_out, reencode, fps, every, min_length):
    """Cut on every N-th beat — music-synced edits."""
    y, sr, duration = _load_and_analyze(input_path)

    console.print("[cyan]Detecting beats…[/cyan]")
    tempo, beat_times = bd.detect_beats(y, sr)
    console.print(f"[green]✓[/green] Tempo: [bold]{tempo:.1f} BPM[/bold]  |  {len(beat_times)} beats found")

    segments = cg.cuts_from_beats(beat_times, duration, every_n_beats=every, min_clip_length=min_length)
    _preview_segments(segments, f"Beat cuts (every {every} beats)")
    _do_export(segments, input_path, output, edl, json_out, reencode, fps)


@cli.command()
@shared_options
@click.option("--threshold", default=0.5, show_default=True, help="Sensitivity 0.0–1.0 (higher = fewer, louder-only hits)")
@click.option("--clip-length", default=3.0, show_default=True, help="Length of each clip in seconds")
@click.option("--pre-roll", default=0.1, show_default=True, help="Seconds before the onset to start clip")
def onset(input_path, output, edl, json_out, reencode, fps, threshold, clip_length, pre_roll):
    """Cut around loud events: applause, drops, chants."""
    y, sr, duration = _load_and_analyze(input_path)

    console.print("[cyan]Detecting onsets…[/cyan]")
    onset_times = bd.detect_onsets(y, sr, threshold=threshold)
    console.print(f"[green]✓[/green] {len(onset_times)} onsets detected")

    segments = cg.cuts_from_onsets(onset_times, duration, pre_roll=pre_roll, clip_length=clip_length)
    _preview_segments(segments, "Onset clips")
    _do_export(segments, input_path, output, edl, json_out, reencode, fps)


@cli.command()
@shared_options
@click.option("--top-db", default=40.0, show_default=True, help="Silence threshold in dB below peak")
@click.option("--min-length", default=1.5, show_default=True, help="Minimum clip length in seconds")
def silence(input_path, output, edl, json_out, reencode, fps, top_db, min_length):
    """Cut at natural silence gaps — clean, non-musical edits."""
    y, sr, duration = _load_and_analyze(input_path)

    console.print("[cyan]Detecting silence…[/cyan]")
    gaps = bd.detect_silence(y, sr, top_db=top_db)
    console.print(f"[green]✓[/green] {len(gaps)} silence gaps found")

    segments = cg.cuts_from_silence(gaps, duration, min_clip_length=min_length)
    _preview_segments(segments, "Silence-based cuts")
    _do_export(segments, input_path, output, edl, json_out, reencode, fps)


@cli.command()
@shared_options
@click.option("--top", default=10, show_default=True, help="Number of highlight clips to extract")
@click.option("--clip-length", default=5.0, show_default=True, help="Length of each highlight clip in seconds")
def highlight(input_path, output, edl, json_out, reencode, fps, top, clip_length):
    """Extract top-N highest-energy moments — instant highlight reel."""
    y, sr, duration = _load_and_analyze(input_path)

    console.print("[cyan]Finding energy peaks…[/cyan]")
    peaks = bd.get_energy_peaks(y, sr, top_n=top * 3)

    # Build fixed-length clips around peaks
    segments = []
    for t in peaks:
        start = max(0.0, t - clip_length / 2)
        end = min(duration, start + clip_length)
        segments.append((round(start, 3), round(end, 3)))
    segments = cg.select_highlights(segments, peaks, top_n=top)
    segments.sort(key=lambda x: x[0])  # chronological order

    _preview_segments(segments, f"Top {top} highlights")
    _do_export(segments, input_path, output, edl, json_out, reencode, fps)


if __name__ == "__main__":
    cli()
