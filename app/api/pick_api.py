"""Native macOS file/folder picker endpoints — delegates to osascript via pick.py."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import pick as pk

router = APIRouter()


@router.get("/pick/file")
async def pick_file(prompt: str = "เลือกไฟล์", location: str = ""):
    loc = Path(location) if location else None
    try:
        path = await asyncio.to_thread(pk.choose_file_native, prompt, None, loc)
        return {"path": str(path)}
    except pk.PickCancelled:
        return {"cancelled": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/pick/files")
async def pick_files(prompt: str = "เลือกไฟล์", location: str = ""):
    loc = Path(location) if location else None
    try:
        paths = await asyncio.to_thread(pk.choose_files_native, prompt, loc)
        return {"paths": [str(p) for p in paths]}
    except pk.PickCancelled:
        return {"cancelled": True, "paths": []}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/pick/folder")
async def pick_folder(prompt: str = "เลือก Folder", location: str = ""):
    loc = Path(location) if location else None
    try:
        path = await asyncio.to_thread(pk.choose_folder_native, prompt, loc)
        return {"path": str(path)}
    except pk.PickCancelled:
        return {"cancelled": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
