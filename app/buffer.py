"""Agrupación de mensajes seguidos.

La gente suele escribir en ráfagas ("hola", "buenas", "una pregunta...").
Esperamos unos segundos y respondemos una sola vez con todo el contexto.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

FlushHandler = Callable[[str, list[str]], Awaitable[None]]


class Debouncer:
    def __init__(self, delay: float, flush: FlushHandler) -> None:
        self._delay = max(0.0, delay)
        self._flush = flush
        self._pending: dict[str, list[str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def add(self, key: str, text: str) -> None:
        self._pending.setdefault(key, []).append(text)

        previous = self._tasks.pop(key, None)
        if previous is not None:
            previous.cancel()

        if self._delay <= 0:
            texts = self._pending.pop(key, [])
            asyncio.create_task(self._run(key, texts))
            return

        self._tasks[key] = asyncio.create_task(self._wait(key))

    async def _wait(self, key: str) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return

        self._tasks.pop(key, None)
        texts = self._pending.pop(key, [])
        if texts:
            await self._run(key, texts)

    async def _run(self, key: str, texts: list[str]) -> None:
        if not texts:
            return
        try:
            await self._flush(key, texts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error procesando mensajes de %s", key)

    def pending(self, key: str) -> int:
        return len(self._pending.get(key, []))

    async def cancel_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._pending.clear()
