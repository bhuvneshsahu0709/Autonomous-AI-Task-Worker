"""In-process event bus backing the console's live stream.

Publishing is synchronous and non-blocking (``put_nowait``) because it is called
from inside tool code, which must never be slowed down or made failable by the
fact that somebody is watching.

Every event is also retained per run, so a console that connects late - or
reloads - replays the whole run instead of starting mid-story.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

MAX_HISTORY = 2000
QUEUE_MAXSIZE = 1000


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._seq = 0

    def publish(self, run_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        self._seq += 1
        event = {
            "seq": self._seq,
            "run_id": run_id,
            "type": event_type,
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "data": data or {},
        }

        history = self._history[run_id]
        history.append(event)
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]

        for queue in list(self._subscribers[run_id]):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A console that cannot keep up is the console's problem; the
                # run must not stall or fail because of it.
                log.warning("Dropping event for run %s: subscriber queue full", run_id)

    def history(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._history[run_id])

    async def stream(self, run_id: str, replay: bool = True) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers[run_id].append(queue)
        try:
            if replay:
                for event in self.history(run_id):
                    yield event
            while True:
                yield await queue.get()
        finally:
            try:
                self._subscribers[run_id].remove(queue)
            except ValueError:
                pass

    def forget(self, run_id: str) -> None:
        self._history.pop(run_id, None)
        self._subscribers.pop(run_id, None)


BUS = EventBus()
