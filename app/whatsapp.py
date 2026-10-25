"""Cliente mínimo para enviar mensajes con WhatsApp Cloud API."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"
MAX_TEXT_LENGTH = 4000  # WhatsApp acepta hasta 4096 caracteres.


class WhatsAppError(RuntimeError):
    """Error devuelto por la API de WhatsApp."""


class WhatsAppClient:
    def __init__(
        self,
        *,
        token: str,
        phone_number_id: str,
        api_version: str = "v21.0",
        timeout: float = 20.0,
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def _messages_url(self) -> str:
        return f"{GRAPH_URL}/{self._api_version}/{self._phone_number_id}/messages"

    async def _post(self, payload: dict) -> dict:
        if not self._token or not self._phone_number_id:
            raise WhatsAppError(
                "Faltan WA_TOKEN o WA_PHONE_NUMBER_ID en la configuración."
            )

        response = await self._client.post(
            self._messages_url,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if response.status_code >= 400:
            raise WhatsAppError(f"{response.status_code}: {response.text[:500]}")
        return response.json()

    async def send_text(self, to: str, body: str) -> dict:
        text = body.strip()
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH].rsplit(" ", 1)[0].rstrip() + "…"
        log.info("→ %s (%s caracteres)", to, len(text))
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            }
        )

    async def mark_as_read(self, message_id: str) -> None:
        await self._post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            }
        )

    async def aclose(self) -> None:
        await self._client.aclose()
