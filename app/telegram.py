"""Canal de Telegram (Bot API): envío, webhook y polling.

Permite probar el asistente sin número de WhatsApp y sin URL pública:
con `TELEGRAM_MODE=polling` el propio servidor consulta a Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .payload import IncomingMessage

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_TEXT_LENGTH = 4000  # Telegram admite 4096.

# Tipos de adjunto de Telegram → tipos normalizados
_MEDIA_MAP = {
    "photo": "image",
    "audio": "audio",
    "voice": "audio",
    "video": "video",
    "video_note": "video",
    "document": "document",
    "sticker": "sticker",
}

_TEXT_FIELDS = ("text", "caption")


class TelegramError(RuntimeError):
    """Error devuelto por la Bot API."""


class TelegramClient:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self._token = token
        self._base = f"{API_BASE}/bot{token}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _call(
        self, method: str, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        if not self._token:
            raise TelegramError("Falta TELEGRAM_BOT_TOKEN en la configuración.")
        try:
            response = await self._client.post(
                f"{self._base}/{method}",
                json=payload or {},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"Error de red en {method}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: respuesta no JSON ({response.status_code})") from exc

        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'error desconocido')}")
        return data.get("result")

    async def send_message(self, chat_id: str | int, text: str, html: bool = True) -> Any:
        body = text.strip()[:MAX_TEXT_LENGTH]
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": body,
            "disable_web_page_preview": True,
        }
        if html:
            payload["parse_mode"] = "HTML"
        log.info("→ telegram %s (%s caracteres)", chat_id, len(body))
        return await self._call("sendMessage", payload)

    async def send_typing(self, chat_id: str | int) -> None:
        try:
            await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            log.debug("No se pudo enviar el indicador de escritura a %s", chat_id)

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout=timeout + 10)
        return result or []

    async def set_webhook(
        self, url: str, secret_token: str | None = None, drop_pending: bool = True
    ) -> Any:
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": ["message"],
            "drop_pending_updates": drop_pending,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return await self._call("setWebhook", payload)

    async def delete_webhook(self, drop_pending: bool = False) -> Any:
        return await self._call("deleteWebhook", {"drop_pending_updates": drop_pending})

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# Normalización de updates
# --------------------------------------------------------------------------- #
def parse_update(update: dict[str, Any]) -> IncomingMessage | None:
    """Convierte un `Update` de Telegram en un IncomingMessage."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")

    if chat_id is None or chat.get("type") != "private":
        return None
    if sender.get("is_bot"):
        return None

    message_id = message.get("message_id")
    text = ""
    for field in _TEXT_FIELDS:
        value = message.get(field)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break

    media_type = None
    for field, normalized in _MEDIA_MAP.items():
        if field in message:
            media_type = normalized
            break

    command = None
    kind = "unsupported"
    if text.startswith("/"):
        command = text[1:].split()[0].split("@")[0].lower() if len(text) > 1 else ""
        kind = "text"
    elif text:
        kind = "text"
    elif media_type:
        kind = "media"

    return IncomingMessage(
        chat_key=f"tg:{chat_id}",
        channel="telegram",
        reply_to=str(chat_id),
        wa_id=f"tg:{chat_id}:{message_id}",
        kind=kind,
        text=text,
        name=" ".join(
            part for part in (sender.get("first_name"), sender.get("last_name")) if part
        )
        or sender.get("username"),
        timestamp=str(message.get("date") or ""),
        media_type=media_type,
        command=command,
    )


# --------------------------------------------------------------------------- #
# Polling (sin URL pública)
# --------------------------------------------------------------------------- #
async def poll_updates(stop: asyncio.Event, client: TelegramClient, on_message) -> None:
    """Bucle de long polling. `on_message` es async y recibe un IncomingMessage."""
    try:
        me = await client.get_me()
        log.info("Telegram conectado como @%s", me.get("username"))
        await client.delete_webhook()
    except TelegramError as exc:
        log.error("No se pudo iniciar el polling de Telegram: %s", exc)
        return

    offset = 0
    while not stop.is_set():
        try:
            updates = await client.get_updates(offset=offset, timeout=25)
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            log.warning("Polling de Telegram: %s (reintento en 5 s)", exc)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = parse_update(update)
            if message is None:
                continue
            try:
                await on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error procesando update de Telegram")
