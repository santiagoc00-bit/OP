"""Base de conocimiento en Markdown, recargada automáticamente al editarla."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class KnowledgeBase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._text = ""
        self._mtime: float | None = None

    @property
    def mtime(self) -> float | None:
        return self._mtime

    def text(self) -> str:
        """Contenido actual del archivo (se relee si cambió en disco)."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None:
                log.error("No se encontró la base de conocimiento: %s", self.path)
            self._text = ""
            self._mtime = None
            return ""

        if mtime != self._mtime:
            try:
                self._text = self.path.read_text(encoding="utf-8")
            except OSError:
                log.exception("No se pudo leer %s", self.path)
                return self._text
            self._mtime = mtime
            log.info(
                "Base de conocimiento cargada: %s (%s caracteres)",
                self.path.name,
                len(self._text),
            )

        return self._text
