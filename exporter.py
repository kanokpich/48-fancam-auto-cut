"""
Export cut list as:
  - Direct FFmpeg concat (MP4 output)
  - EDL file (import into DaVinci Resolve / Premiere)
  - JSON (for debugging or piping into other tools)
"""

import json
import subprocess
import tempfile
from pathlib import Path


def export_ffmpeg(
    input_path: str,
    segments: list[tuple[float, float]],
    output_path: str,
    reencode: bool = False,
) -> None:
    """
    Cut segments and concatenate into a single output file.
    reencode=False uses stream copy (fast, no quality loss) but requires keyframe-aligned cuts.
    reencode=True is accurate to the frame but slower.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_list = f.name
        for i, (start, end) in enumerate(segments):
            seg_path = output_path.parent / f"_seg_{i:04d}{input_path.suffix}"
            _cut_segment(str(input_path), start, end, str(seg_path), reencode)
            f.write(f"file '{seg_path.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    # Clean up segment files
    for i in range(len(segments)):
        seg_path = output_path.parent / f"_seg_{i:04d}{input_path.suffix}"
        seg_path.unlink(missing_ok=True)
    Path(concat_list).unlink(missing_ok=True)


def _cut_segment(
    input_path: str, start: float, end: float, output_path: str, reencode: bool
) -> None:
    duration = end - start
    codec_flags = ["-c:v", "libx264", "-c:a", "aac", "-preset", "fast"] if reencode else ["-c", "copy"]
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", str(duration),
        *codec_flags,
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def export_edl(
    segments: list[tuple[float, float]],
    output_path: str,
    title: str = "AUTO_CUT",
    source_name: str = "INPUT",
    fps: float = 29.97,
) -> None:
    """
    Export CMX3600 EDL — importable into DaVinci Resolve, Premiere, FCPX.
    """

    def to_timecode(seconds: float, fps: float) -> str:
        total_frames = round(seconds * fps)
        h = total_frames // (3600 * round(fps))
        m = (total_frames % (3600 * round(fps))) // (60 * round(fps))
        s = (total_frames % (60 * round(fps))) // round(fps)
        f = total_frames % round(fps)
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    record_start = 0.0

    for i, (src_start, src_end) in enumerate(segments, start=1):
        duration = src_end - src_start
        rec_end = record_start + duration
        lines += [
            f"{i:03d}  {source_name:<8} V     C        "
            f"{to_timecode(src_start, fps)} {to_timecode(src_end, fps)} "
            f"{to_timecode(record_start, fps)} {to_timecode(rec_end, fps)}",
            f"* FROM CLIP NAME: {source_name}",
            "",
        ]
        record_start = rec_end

    Path(output_path).write_text("\n".join(lines))


def export_json(segments: list[tuple[float, float]], output_path: str) -> None:
    data = [{"start": s, "end": e, "duration": round(e - s, 3)} for s, e in segments]
    Path(output_path).write_text(json.dumps(data, indent=2))
