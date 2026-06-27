"""In-memory job registry. One asyncio Queue per job drains to the WebSocket."""
from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

_main_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


@dataclass
class Job:
    id: str
    kind: str
    status: str = "pending"
    result: Any = None
    error: str = ""
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def push(self, event_type: str, payload: Any = None) -> None:
        if _main_loop and not _main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.queue.put({"type": event_type, "payload": payload}),
                _main_loop,
            )


_registry: dict[str, Job] = {}
_lock = threading.Lock()


def create(kind: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:8], kind=kind)
    with _lock:
        _registry[job.id] = job
    return job


def get(jid: str) -> Job | None:
    return _registry.get(jid)


def snapshot() -> list[dict]:
    with _lock:
        return [
            {"id": j.id, "kind": j.kind, "status": j.status, "error": j.error}
            for j in _registry.values()
        ]
