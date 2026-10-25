"""Modelo común de mensajes entrantes, independiente del canal.

WhatsApp y Telegram se normalizan a esta estructura para que el resto del
sistema (IA, historial, escalado) no sepa de qué canal vienen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tipos que sabemos interpretar como texto.
_TEXT_TYPES = {"text", "button", "interactive"}

# Tipos que el cliente envió pero que no podemos leer: mejor pasarlos a un humano.
_MEDIA_TYPES = {"image", "audio", "video", "document", "sticker"}

# Ruido que no amerita respuesta (reacciones, mensajes de sistema, etc.).
_IGNORED_TYPES = {
    "reaction",
    "system",
    "ephemeral",
    "request_welcome",
    "order",
    "unknown",
}

MEDIA_LABELS = {
    "image": "una imagen",
    "audio": "un audio",
    "video": "un video",
    "document": "un documento",
    "sticker": "un sticker",
}


@dataclass(slots=True)
class IncomingMessage:
    chat_key: str  # identificador interno: teléfono (WhatsApp) o "tg:<id>" (Telegram)
    channel: str  # "whatsapp" | "telegram"
    reply_to: str  # destino real al que se responde en ese canal
    wa_id: str  # id único del mensaje en el canal (para deduplicar)
    kind: str  # "text" | "media" | "unsupported"
    text: str
    name: str | None = None
    timestamp: str | None = None
    media_type: str | None = None
    command: str | None = None  # solo Telegram: "/start" → "start"


def extract_messages(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Mensajes entrantes de un payload de WhatsApp Cloud API."""
    messages: list[IncomingMessage] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }
            for raw in value.get("messages") or []:
                parsed = _parse_whatsapp_message(raw, names)
                if parsed is not None:
                    messages.append(parsed)

    return messages


def _parse_whatsapp_message(
    raw: dict[str, Any], names: dict[str | None, str | None]
) -> IncomingMessage | None:
    wa_id = raw.get("id")
    phone = raw.get("from")
    msg_type = (raw.get("type") or "").lower()

    if not wa_id or not phone or msg_type in _IGNORED_TYPES:
        return None

    base = {
        "chat_key": phone,
        "channel": "whatsapp",
        "reply_to": phone,
        "wa_id": wa_id,
        "name": names.get(phone),
        "timestamp": raw.get("timestamp"),
    }

    if msg_type == "text":
        return IncomingMessage(
            kind="text",
            text=((raw.get("text") or {}).get("body") or "").strip(),
            **base,
        )

    if msg_type == "button":
        return IncomingMessage(
            kind="text",
            text=((raw.get("button") or {}).get("text") or "").strip(),
            **base,
        )

    if msg_type == "interactive":
        interactive = raw.get("interactive") or {}
        option = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return IncomingMessage(
            kind="text",
            text=(option.get("title") or "").strip(),
            **base,
        )

    if msg_type in _MEDIA_TYPES:
        return IncomingMessage(kind="media", text="", media_type=msg_type, **base)

    return IncomingMessage(kind="unsupported", text="", **base)


def stored_text(message: IncomingMessage) -> str:
    """Cómo se guarda el mensaje en el historial (y lo que ve el humano)."""
    if message.kind == "text":
        return message.text
    if message.kind == "media":
        label = MEDIA_LABELS.get(message.media_type or "", "un archivo")
        return f"[El cliente envió {label}]"
    return "[El cliente envió un mensaje que el bot no puede leer]"
