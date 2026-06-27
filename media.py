"""
Media + external-volume helpers.

Source files for idol shoots normally live on an external SSD or SD card under
/Volumes/<NAME>/... on macOS. These helpers resolve paths, verify the drive is
actually mounted (clear error if not), probe media, and optionally cache files
to a local working dir when the card is slow or you'll iterate a lot.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

VOLUMES_ROOT = Path("/Volumes")


# ── External volumes ────────────────────────────────────────────────────────

def list_volumes() -> list[Path]:
    """All mounted volumes under /Volumes (external drives + boot disk)."""
    if not VOLUMES_ROOT.exists():
        return []
    return sorted(p for p in VOLUMES_ROOT.iterdir() if p.is_dir())


def resolve_source(path: str | Path) -> Path:
    """
    Expand and validate a source path. Raises a friendly error if a file on an
    external volume is missing — usually means the SD card / SSD isn't plugged in.
    """
    p = Path(path).expanduser()
    if p.exists():
        return p.resolve()

    # Figure out whether this was supposed to be on an external volume.
    parts = p.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        vol = Path("/Volumes") / parts[2]
        if not vol.exists():
            mounted = ", ".join(v.name for v in list_volumes()) or "(none)"
            raise FileNotFoundError(
                f"ไดรฟ์ '{parts[2]}' ไม่ได้เสียบอยู่ (not mounted).\n"
                f"  ที่เสียบอยู่ตอนนี้: {mounted}\n"
                f"  เสียบ SD card / SSD แล้วลองใหม่"
            )
    raise FileNotFoundError(f"ไม่เจอไฟล์: {p}")


def cache_local(path: str | Path, workdir: str | Path) -> Path:
    """
    Copy a source file into a local working dir and return the local path.
    Use when reading repeatedly from a slow SD card. Skips copy if already cached
    and the size matches.
    """
    src = resolve_source(path)
    work = Path(workdir).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    dst = work / src.name
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return dst
    shutil.copy2(src, dst)
    return dst


# ── ffprobe ─────────────────────────────────────────────────────────────────

@dataclass
class MediaInfo:
    path: Path
    duration: float          # seconds
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    fps: float | None
    v_codec: str | None
    a_codec: str | None
    sample_rate: int | None


def probe(path: str | Path) -> MediaInfo:
    """Probe a media file via ffprobe. Works for both .mov and .wav."""
    p = resolve_source(path)
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate",
        "-of", "json", str(p),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    data = json.loads(out)

    duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    width = height = fps = v_codec = a_codec = sample_rate = None
    has_video = has_audio = False

    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not has_video:
            has_video = True
            width = s.get("width")
            height = s.get("height")
            v_codec = s.get("codec_name")
            fps = _parse_fraction(s.get("r_frame_rate"))
        elif s.get("codec_type") == "audio" and not has_audio:
            has_audio = True
            a_codec = s.get("codec_name")
            sr = s.get("sample_rate")
            sample_rate = int(sr) if sr else None

    return MediaInfo(p, duration, has_video, has_audio, width, height, fps,
                     v_codec, a_codec, sample_rate)


def extract_audio(video_or_audio: str | Path, out_wav: str | Path,
                  sr: int = 22050) -> Path:
    """
    Extract a mono WAV at the given sample rate (used for sync analysis).
    Works on both .mov (scratch track) and .wav.
    """
    src = resolve_source(video_or_audio)
    out = Path(out_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sr),
        "-f", "wav", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _parse_fraction(frac: str | None) -> float | None:
    if not frac:
        return None
    try:
        if "/" in frac:
            num, den = frac.split("/")
            den = float(den)
            return float(num) / den if den else None
        return float(frac)
    except (ValueError, ZeroDivisionError):
        return None
