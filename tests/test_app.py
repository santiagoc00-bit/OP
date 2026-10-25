"""Tests de la lógica que no depende de servicios externos."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.config import settings
from app.format import to_telegram, to_whatsapp
from app.payload import extract_messages, stored_text
from app.security import valid_signature
from app.telegram import parse_update

SECRET = "secreto-de-prueba"
VERIFY_TOKEN = "token-de-prueba"


# --------------------------------------------------------------------------- #
# Firma de webhooks
# --------------------------------------------------------------------------- #
def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_firma_valida() -> None:
    body = b'{"hola": "mundo"}'
    assert valid_signature(SECRET, body, _sign(body))


def test_firma_invalida() -> None:
    body = b'{"hola": "mundo"}'
    assert not valid_signature(SECRET, body, _sign(body, "otro-secreto"))
    assert not valid_signature(SECRET, body, None)
    assert not valid_signature(SECRET, b'{"otro": 1}', _sign(body))


def test_sin_secreto_no_valida() -> None:
    assert valid_signature("", b"x", None)


# --------------------------------------------------------------------------- #
# Payload de WhatsApp
# --------------------------------------------------------------------------- #
def _payload(wa_id: str = "wamid.ABC", body: str = "Hola, ¿abren hoy?") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [
                                {
                                    "profile": {"name": "Ana"},
                                    "wa_id": "5215512345678",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "5215512345678",
                                    "id": wa_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_extrae_mensaje_de_texto() -> None:
    messages = extract_messages(_payload())
    assert len(messages) == 1
    message = messages[0]
    assert message.chat_key == "5215512345678"
    assert message.channel == "whatsapp"
    assert message.reply_to == "5215512345678"
    assert message.text == "Hola, ¿abren hoy?"
    assert message.name == "Ana"
    assert message.kind == "text"
    assert stored_text(message) == "Hola, ¿abren hoy?"


def test_ignora_reacciones_y_estados() -> None:
    payload = _payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "reaction"
    assert extract_messages(payload) == []

    statuses = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}
    assert extract_messages(statuses) == []


def test_etiqueta_multimedia() -> None:
    payload = _payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "5215512345678",
        "id": "wamid.IMG",
        "timestamp": "1700000000",
        "type": "image",
        "image": {"id": "media-1"},
    }
    message = extract_messages(payload)[0]
    assert message.kind == "media"
    assert message.media_type == "image"
    assert "imagen" in stored_text(message)


# --------------------------------------------------------------------------- #
# Formato para WhatsApp
# --------------------------------------------------------------------------- #
def test_convierte_markdown() -> None:
    result = to_whatsapp("## Horario\n\n**Lunes** de 7 a 20\n- Sábado\n- Domingo")
    assert "##" not in result
    assert "**" not in result
    assert "*Lunes*" in result
    assert "• Sábado" in result


def test_no_corta_palabras_al_truncar() -> None:
    result = to_whatsapp("palabra " * 1000, limit=50)
    assert len(result) <= 51
    assert result.endswith("…")


# --------------------------------------------------------------------------- #
# Base de datos
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db(tmp_path):
    db.init(tmp_path / "test.db")
    yield tmp_path / "test.db"


def test_deduplica_webhooks(temp_db) -> None:
    assert db.register_incoming("wamid.1", "52155", "hola", "Ana") is True
    assert db.register_incoming("wamid.1", "52155", "hola", "Ana") is False

    history = db.get_history("52155")
    assert len(history) == 1


def test_historial_en_orden_y_pausa(temp_db) -> None:
    db.register_incoming("wamid.1", "52155", "hola")
    db.add_message("52155", "assistant", "¡Hola! ¿En qué te ayudo?")
    db.register_incoming("wamid.2", "52155", "quiero un café")

    history = db.get_history("52155")
    assert [m["role"] for m in history] == ["user", "assistant", "user"]

    assert db.is_paused("52155") is False
    db.set_paused("52155", True, "reclamo")
    assert db.is_paused("52155") is True
    assert db.get_conversation("52155")["pause_reason"] == "reclamo"

    db.set_paused("52155", False)
    assert db.is_paused("52155") is False
    assert db.get_conversation("52155")["pause_reason"] is None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", SECRET)
    monkeypatch.setattr(settings, "wa_verify_token", VERIFY_TOKEN)
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "buffer_seconds", 0)
    with TestClient(main.app) as test_client:
        yield test_client


def test_handshake_correcto(client) -> None:
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_handshake_rechazado(client) -> None:
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "incorrecto", "hub.challenge": "x"},
    )
    assert response.status_code == 403


def test_webhook_rechaza_firma_invalida(client) -> None:
    body = json.dumps(_payload()).encode()
    response = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"}
    )
    assert response.status_code == 401


def test_webhook_procesa_una_sola_vez(client, monkeypatch) -> None:
    procesados: list[str] = []

    async def fake_dispatch(message):
        procesados.append(message.wa_id)

    monkeypatch.setattr(main, "_dispatch", fake_dispatch)

    body = json.dumps(_payload(wa_id="wamid.UNO")).encode()
    headers = {"X-Hub-Signature-256": _sign(body)}

    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"status": "ok"}
    assert procesados == ["wamid.UNO"]


def test_admin_requiere_clave(client) -> None:
    assert client.get("/admin/conversations").status_code == 401
    ok = client.get("/admin/conversations", headers={"X-Admin-Key": settings.admin_key})
    assert ok.status_code == 200
    assert ok.json() == {"items": []}


def test_palabras_de_reanudacion() -> None:
    assert main._is_resume("Menú")
    assert main._is_resume(" BOT. ")
    assert not main._is_resume("quiero el menú del día")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def _tg_update(
    *,
    update_id: int = 1,
    chat_id: int = 123456,
    message_id: int = 10,
    text: str | None = "Hola",
    chat_type: str = "private",
    **extra: object,
) -> dict:
    message: dict = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Ana"},
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": update_id, "message": message}


def test_telegram_texto() -> None:
    message = parse_update(_tg_update())
    assert message is not None
    assert message.channel == "telegram"
    assert message.chat_key == "tg:123456"
    assert message.reply_to == "123456"
    assert message.text == "Hola"
    assert message.kind == "text"
    assert message.wa_id == "tg:123456:10"


def test_telegram_foto() -> None:
    message = parse_update(_tg_update(text=None, photo=[{"file_id": "abc"}]))
    assert message is not None
    assert message.kind == "media"
    assert message.media_type == "image"
    assert "imagen" in stored_text(message)


def test_telegram_ignora_grupos_y_bots() -> None:
    assert parse_update(_tg_update(chat_type="group")) is None
    update = _tg_update()
    update["message"]["from"]["is_bot"] = True
    assert parse_update(update) is None
    assert parse_update({"update_id": 5}) is None


def test_telegram_comandos() -> None:
    start = parse_update(_tg_update(text="/start"))
    assert start is not None and start.command == "start"

    with_username = parse_update(_tg_update(text="/menu@MiBot"))
    assert with_username is not None and with_username.command == "menu"


def test_formato_telegram_escapa_html() -> None:
    result = to_telegram("**Hola** <b> & 5 > 3")
    assert result == "<b>Hola</b> &lt;b&gt; &amp; 5 &gt; 3"


def test_envia_por_telegram_con_formato(temp_db) -> None:
    db.register_incoming(
        "tg:123456:1", "tg:123456", "hola", "Ana", channel="telegram", reply_to="123456"
    )

    class FakeTelegram:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, chat_id, text, html=True):
            self.sent.append((chat_id, text))

    fake = FakeTelegram()
    original = main._telegram
    main._telegram = fake  # type: ignore[assignment]
    try:
        assert asyncio.run(main._send("tg:123456", "**Hola** & <adios>")) is True
    finally:
        main._telegram = original  # type: ignore[assignment]

    assert fake.sent == [("123456", "<b>Hola</b> &amp; &lt;adios&gt;")]
    # En el historial se guarda el texto original, no el HTML.
    assert db.get_messages("tg:123456")[-1]["content"] == "**Hola** & <adios>"


def test_telegram_webhook_valida_secreto_y_deduplica(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secreto-tg")

    procesados: list[str] = []

    async def fake_dispatch(message):
        procesados.append(message.wa_id)

    monkeypatch.setattr(main, "_dispatch", fake_dispatch)

    update = _tg_update()

    assert client.post("/telegram/webhook", json=update).status_code == 401

    headers = {"X-Telegram-Bot-Api-Secret-Token": "secreto-tg"}
    first = client.post("/telegram/webhook", json=update, headers=headers)
    second = client.post("/telegram/webhook", json=update, headers=headers)

    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert second.status_code == 200
    assert procesados == ["tg:123456:10"]


def test_telegram_webhook_sin_secreto_configurado(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")

    async def fake_dispatch(message):
        return None

    monkeypatch.setattr(main, "_dispatch", fake_dispatch)

    response = client.post("/telegram/webhook", json=_tg_update(update_id=99, message_id=99))
    assert response.status_code == 200


def test_polling_entrega_updates(temp_db) -> None:
    """El modo polling (sin URL pública) pasa los mensajes al mismo flujo."""
    from app.telegram import poll_updates

    stop = asyncio.Event()
    recibidos: list[str] = []

    class FakeTelegram:
        def __init__(self) -> None:
            self.calls = 0

        async def get_me(self):
            return {"username": "mi_bot"}

        async def delete_webhook(self, drop_pending=False):
            return True

        async def get_updates(self, offset=None, timeout=25):
            self.calls += 1
            if self.calls == 1:
                return [_tg_update(update_id=7, message_id=70, text="Hola")]
            stop.set()
            return []

    async def handler(message):
        recibidos.append(message.text)

    asyncio.run(poll_updates(stop, FakeTelegram(), handler))
    assert recibidos == ["Hola"]
