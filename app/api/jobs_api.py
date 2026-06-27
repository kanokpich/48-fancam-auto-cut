"""Job status polling and WebSocket event stream."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app import jobs as _jobs

router = APIRouter()


@router.get("/jobs")
def list_jobs():
    return _jobs.snapshot()


@router.get("/jobs/{jid}")
def get_job(jid: str):
    job = _jobs.get(jid)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "error": job.error,
        "result": job.result,
    }


@router.websocket("/jobs/{jid}/ws")
async def job_ws(websocket: WebSocket, jid: str):
    job = _jobs.get(jid)
    if not job:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            try:
                event = await asyncio.wait_for(job.queue.get(), timeout=20.0)
                await websocket.send_json(event)
                if event["type"] in ("done", "failed"):
                    break
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
