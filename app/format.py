"""Limpieza del texto del modelo para que se vea bien en WhatsApp."""

from __future__ import annotations

import re

_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE_FENCE = re.compile(r"```[a-zA-Z0-9]*\n?")
_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_BLANK_LINES = re.compile(r"\n{3,}")
_TG_BOLD = re.compile(r"\*([^*\n]+)\*")
_HTML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def to_whatsapp(text: str, limit: int = 4000) -> str:
    """Convierte markdown genérico al formato de WhatsApp."""
    result = (text or "").replace("\r\n", "\n").strip()
    if not result:
        return ""

    result = _CODE_FENCE.sub("", result)
    result = _HEADER.sub("", result)
    result = _BOLD.sub(r"*\1*", result)
    result = _BULLET.sub("• ", result)
    result = _BLANK_LINES.sub("\n\n", result).strip()

    if len(result) > limit:
        result = result[:limit].rsplit(" ", 1)[0].rstrip() + "…"

    return result


def to_telegram(text: str, limit: int = 4000) -> str:
    """Convierte el texto del modelo a HTML válido para Telegram.

    Telegram interpreta `*texto*` como MarkdownV2, que exige escapar casi todo.
    Es más robusto enviar HTML: escapamos los caracteres peligrosos y
    traducimos la negrita de WhatsApp (`*texto*`) a `<b>texto</b>`.
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""

    if len(raw) > limit:
        raw = raw[:limit].rsplit(" ", 1)[0].rstrip() + "…"

    # El modelo a veces devuelve **negrita** (markdown) y a veces *negrita* (WhatsApp).
    raw = _BOLD.sub(r"*\1*", raw)
    escaped = raw.translate(_HTML_ESCAPES)
    return _TG_BOLD.sub(r"<b>\1</b>", escaped)
