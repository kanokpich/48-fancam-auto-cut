"""Volume listing, server-side file browser, and local file serving."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

import media

router = APIRouter()

_KIND_EXTS: dict[str, set[str] | None] = {
    "video": {"mov", "mp4", "m4v", "avi", "mxf"},
    "audio": {"wav", "aif", "aiff", "flac"},
    "json":  {"json"},
    "image": {"png", "jpg", "jpeg", "gif"},
    "all":   None,
}


@router.get("/volumes")
def list_volumes():
    return [{"path": str(v), "name": v.name} for v in media.list_volumes()]


@router.get("/browse")
def browse(path: str, kind: str = "all"):
    exts = _KIND_EXTS.get(kind)
    p = Path(path)
    if not p.exists():
        return []
    result: list[dict] = []
    try:
        for child in sorted(p.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                result.append({"path": str(child), "name": child.name,
                                "is_dir": True, "size": 0})
            elif exts is None or child.suffix.lower().lstrip(".") in exts:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                result.append({"path": str(child), "name": child.name,
                                "is_dir": False, "size": size})
    except PermissionError:
        pass
    return result


@router.get("/file")
def serve_file(path: str):
    """Serve any local file (video preview, image, etc.) — localhost only, no auth needed."""
    return FileResponse(path, media_type=None)
