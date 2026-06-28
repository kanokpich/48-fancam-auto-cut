"""FastAPI application — 127.0.0.1 only, single-user local tool."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make the auto-cut root importable from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import jobs as _jobs
from app.api.files import router as files_router
from app.api.pipeline import router as pipeline_router
from app.api.render_api import router as render_router
from app.api.jobs_api import router as jobs_router
from app.api.pick_api import router as pick_router

app = FastAPI(title="auto-cut", docs_url=None, redoc_url=None)

for r in (files_router, pipeline_router, render_router, jobs_router, pick_router):
    app.include_router(r, prefix="/api")

_static = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


@app.on_event("startup")
async def _startup() -> None:
    _jobs.set_loop(asyncio.get_running_loop())


if __name__ == "__main__":
    import webbrowser
    import uvicorn

    webbrowser.open("http://127.0.0.1:8000")
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
