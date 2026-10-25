"""Servidor FastAPI: webhooks de WhatsApp/Telegram + panel de administración.

La lógica de negocio (IA, historial, escalado) es común a todos los canales.
Cada conversación se identifica por una "clave":
  - WhatsApp: el número de teléfono, ej. "5215512345678"
  - Telegram: "tg:<chat_id>", ej. "tg:123456789"
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import db
from .ai import SupportAgent
from .buffer import Debouncer
from .config import settings
from .format import to_telegram, to_whatsapp
from .knowledge import KnowledgeBase
from .payload import IncomingMessage, extract_messages, stored_text
from .security import valid_signature
from .telegram import TelegramClient, TelegramError, parse_update, poll_updates
from .whatsapp import WhatsAppClient, WhatsAppError

log = logging.getLogger("asistente")

RESUME_KEYWORDS = {"menu", "menú", "bot", "inicio", "reiniciar", "empezar", "atras", "atrás"}

START_REPLY = (
    "¡Hola! Soy el asistente de {business} 🤖\n\n"
    "Puedo ayudarte con horarios, precios, envíos, pagos y dudas frecuentes. "
    "Si en algún momento necesitas a una persona, solo pídemelo."
)

RESUMED_REPLY = "¡Listo! Vuelvo a atenderte 🤖 ¿En qué te puedo ayudar?"

UNSUPPORTED_REPLY = (
    "Por ahora solo puedo leer mensajes de texto 🙏 "
    "Le paso lo que enviaste a una persona del equipo para revisarlo."
)

# --------------------------------------------------------------------------- #
# Servicios (creados de forma perezosa para poder arrancar sin credenciales)
# --------------------------------------------------------------------------- #
_kb: KnowledgeBase | None = None
_wa: WhatsAppClient | None = None
_telegram: TelegramClient | None = None
_agent: SupportAgent | None = None
_debouncer: Debouncer | None = None
_poll_task: asyncio.Task[None] | None = None
_stop_polling = asyncio.Event()


def get_kb() -> KnowledgeBase:
    global _kb
    if _kb is None:
        _kb = KnowledgeBase(settings.knowledge_file)
    return _kb


def get_wa() -> WhatsAppClient:
    global _wa
    if _wa is None:
        _wa = WhatsAppClient(
            token=settings.wa_token,
            phone_number_id=settings.wa_phone_number_id,
            api_version=settings.wa_api_version,
        )
    return _wa


def get_telegram() -> TelegramClient:
    global _telegram
    if _telegram is None:
        _telegram = TelegramClient(settings.telegram_bot_token)
    return _telegram


def get_agent() -> SupportAgent:
    global _agent
    if _agent is None:
        _agent = SupportAgent(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            temperature=settings.gemini_temperature,
            max_output_tokens=settings.gemini_max_output_tokens,
            thinking_budget=settings.gemini_thinking_budget,
            timezone=settings.timezone,
        )
    return _agent


def get_debouncer() -> Debouncer:
    global _debouncer
    if _debouncer is None:
        _debouncer = Debouncer(settings.buffer_seconds, _handle_batch)
    return _debouncer


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task

    setup_logging()
    db.init(settings.database_file)
    get_kb().text()
    get_debouncer()

    missing = settings.missing_required()
    if missing:
        log.warning("Faltan variables de entorno: %s", ", ".join(missing))

    channels = settings.active_channels()
    if not channels:
        log.warning("Ningún canal configurado: no se podrán recibir mensajes.")
    else:
        log.info("Canales activos: %s", ", ".join(channels))

    if settings.wa_token and not settings.wa_app_secret:
        log.warning(
            "WA_APP_SECRET vacío: los webhooks de WhatsApp NO se validan. "
            "Configúralo antes de publicar el bot."
        )

    if "telegram" in channels and settings.telegram_mode == "polling":
        if _stop_polling.is_set():
            _stop_polling.clear()
        _poll_task = asyncio.create_task(
            poll_updates(_stop_polling, get_telegram(), _on_telegram_message),
            name="telegram-polling",
        )
        log.info("Telegram en modo polling (no necesita URL pública).")
    elif "telegram" in channels:
        log.info("Telegram en modo webhook: POST /telegram/webhook")

    log.info("Asistente listo. Negocio: %s", settings.business_name)
    yield

    if _poll_task is not None:
        _stop_polling.set()
        _poll_task.cancel()
        try:
            await _poll_task
        except (asyncio.CancelledError, Exception):
            pass
        _poll_task = None

    if _debouncer is not None:
        await _debouncer.cancel_all()
    if _wa is not None:
        await _wa.aclose()
    if _telegram is not None:
        await _telegram.aclose()


app = FastAPI(
    title="Asistente de WhatsApp y Telegram",
    description="Soporte automático con Gemini y escalado a humanos.",
    version="0.2.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Ingesta común
# --------------------------------------------------------------------------- #
async def ingest(message: IncomingMessage) -> bool:
    """Guarda el mensaje y devuelve False si es un duplicado del canal."""
    stored = db.register_incoming(
        message.wa_id,
        message.chat_key,
        stored_text(message),
        message.name,
        channel=message.channel,
        reply_to=message.reply_to,
    )
    if not stored:
        log.info("Mensaje duplicado ignorado: %s", message.wa_id)
    return stored


async def _on_telegram_message(message: IncomingMessage) -> None:
    if await ingest(message):
        await _dispatch(message)


# --------------------------------------------------------------------------- #
# WhatsApp
# --------------------------------------------------------------------------- #
@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> str:
    """Handshake que Meta hace al suscribir el webhook."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.wa_verify_token
    ):
        return params.get("hub.challenge", "")

    log.warning("Verificación de webhook rechazada (token incorrecto)")
    raise HTTPException(status_code=403, detail="Verificación fallida")


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    raw = await request.body()

    if not valid_signature(
        settings.wa_app_secret, raw, request.headers.get("x-hub-signature-256")
    ):
        log.warning("Webhook con firma inválida")
        raise HTTPException(status_code=401, detail="Firma inválida")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        log.warning("Payload no es JSON válido")
        return {"status": "ignored"}

    if payload.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    for message in extract_messages(payload):
        if await ingest(message):
            # Respondemos 200 al instante: si Meta no recibe 200 rápido, reintenta.
            background.add_task(_dispatch, message)

    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    secret = settings.telegram_webhook_secret
    if secret:
        received = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(secret.encode(), received.encode()):
            log.warning("Webhook de Telegram con secreto inválido")
            raise HTTPException(status_code=401, detail="No autorizado")

    try:
        update = await request.json()
    except json.JSONDecodeError:
        return {"ok": True}

    message = parse_update(update)
    if message is not None and await ingest(message):
        background.add_task(_dispatch, message)

    return {"ok": True}


# --------------------------------------------------------------------------- #
# Flujo de conversación
# --------------------------------------------------------------------------- #
async def _dispatch(message: IncomingMessage) -> None:
    """Tareas posteriores a la recepción: marcar leído, comandos y encolado."""
    if message.channel == "telegram":
        await get_telegram().send_typing(message.reply_to)
    elif settings.wa_mark_as_read:
        try:
            await get_wa().mark_as_read(message.wa_id)
        except Exception:
            log.debug("No se pudo marcar como leído %s", message.wa_id)

    # Comandos de Telegram (/start, /menu...). Siempre reactivan el bot.
    if message.command in {"start", "ayuda", "help"}:
        db.set_paused(message.chat_key, False)
        await _send(message.chat_key, START_REPLY.format(business=settings.business_name))
        return
    if message.command in {"menu", "bot", "reiniciar", "reset"}:
        db.set_paused(message.chat_key, False)
        await _send(message.chat_key, RESUMED_REPLY)
        return

    paused = db.is_paused(message.chat_key)
    wants_resume = message.kind == "text" and _is_resume(message.text)

    # Si un humano tiene la conversación, el bot se calla (salvo "menú").
    if paused and not wants_resume:
        log.info("Conversación %s pausada: el bot no responde", message.chat_key)
        return

    if message.kind != "text":
        log.info(
            "Mensaje no textual de %s (%s) → humano", message.chat_key, message.kind
        )
        await _send(message.chat_key, UNSUPPORTED_REPLY)
        db.set_paused(message.chat_key, True, "mensaje multimedia")
        return

    if not message.text:
        return

    get_debouncer().add(message.chat_key, message.text)


async def _handle_batch(chat_key: str, texts: list[str]) -> None:
    """Se ejecuta cuando el cliente deja de escribir (o al instante)."""
    if _is_resume(texts[-1]):
        db.set_paused(chat_key, False)
        await _send(chat_key, RESUMED_REPLY)
        return

    if db.is_paused(chat_key):
        return

    history = db.get_history(chat_key, settings.history_turns * 2)
    if not history:
        return

    answer = await get_agent().answer(
        history=history,
        knowledge=get_kb().text(),
        business_name=settings.business_name,
    )

    body = answer.reply.strip()
    if answer.handoff:
        body = f"{body}\n\n{settings.handoff_message}".strip()

    sent = await _send(chat_key, body)
    if answer.handoff:
        await _escalate(chat_key, answer.handoff_reason or "solicitud del cliente")
    elif not sent:
        log.error("Respuesta no entregada a %s", chat_key)


def _channel_and_target(chat_key: str) -> tuple[str, str]:
    conversation = db.get_conversation(chat_key) or {}
    channel = conversation.get("channel") or (
        "telegram" if chat_key.startswith("tg:") else "whatsapp"
    )
    target = conversation.get("reply_to") or (
        chat_key.split(":", 1)[1] if chat_key.startswith("tg:") else chat_key
    )
    return channel, target


async def _send(chat_key: str, body: str, role: str = "assistant") -> bool:
    if not body.strip():
        return False

    channel, target = _channel_and_target(chat_key)
    try:
        if channel == "telegram":
            await get_telegram().send_message(target, to_telegram(body))
        else:
            await get_wa().send_text(target, to_whatsapp(body))
    except (WhatsAppError, TelegramError) as exc:
        log.error("No se pudo enviar a %s (%s): %s", chat_key, channel, exc)
        return False
    except Exception:
        log.exception("Error inesperado enviando a %s", chat_key)
        return False

    db.add_message(chat_key, role, body)
    return True


async def _escalate(chat_key: str, reason: str) -> None:
    db.set_paused(chat_key, True, reason)
    log.info("Escalado a humano: %s (%s)", chat_key, reason)

    if not settings.handoff_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                settings.handoff_webhook_url,
                json={
                    "business": settings.business_name,
                    "conversation": chat_key,
                    "reason": reason,
                },
            )
    except Exception:
        log.warning("No se pudo notificar el escalado a %s", settings.handoff_webhook_url)


def _is_resume(text: str) -> bool:
    normalized = (text or "").strip().lower().strip(".!¡?¿")
    return normalized in RESUME_KEYWORDS


# --------------------------------------------------------------------------- #
# Administración
# --------------------------------------------------------------------------- #
def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = settings.admin_key.encode("utf-8")
    received = (x_admin_key or "").encode("utf-8")
    if not expected or not secrets.compare_digest(expected, received):
        raise HTTPException(status_code=401, detail="No autorizado")


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "servicio": "Asistente de WhatsApp y Telegram",
        "negocio": settings.business_name,
        "estado": "ok",
        "canales": settings.active_channels(),
        "telegram_modo": settings.telegram_mode,
        "configurado": not settings.missing_required(),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/conversations", dependencies=[Depends(require_admin)])
async def list_conversations(
    paused: bool | None = Query(default=None, description="Filtrar por estado del bot"),
    channel: str | None = Query(default=None, description="whatsapp | telegram"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = db.list_conversations(paused=paused, limit=limit)
    if channel:
        items = [item for item in items if item.get("channel") == channel]
    return {"items": items}


@app.get("/admin/conversations/{chat_key}", dependencies=[Depends(require_admin)])
async def get_conversation(
    chat_key: str, limit: int = Query(default=100, ge=1, le=500)
) -> dict[str, Any]:
    conversation = db.get_conversation(chat_key)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return {"conversation": conversation, "messages": db.get_messages(chat_key, limit)}


@app.post("/admin/conversations/{chat_key}/pause", dependencies=[Depends(require_admin)])
async def pause_conversation(
    chat_key: str, reason: str = Query(default="pausado manualmente")
) -> dict[str, Any]:
    db.set_paused(chat_key, True, reason)
    return {"conversation": chat_key, "paused": True, "reason": reason}


@app.post("/admin/conversations/{chat_key}/resume", dependencies=[Depends(require_admin)])
async def resume_conversation(chat_key: str) -> dict[str, Any]:
    db.set_paused(chat_key, False)
    return {"conversation": chat_key, "paused": False}


@app.post("/admin/send", dependencies=[Depends(require_admin)])
async def admin_send(payload: dict[str, Any]) -> dict[str, Any]:
    """Permite a una persona del equipo escribir desde el panel.

    `to` es la clave de conversación: el teléfono (WhatsApp) o "tg:<id>" (Telegram).
    """
    to = str(payload.get("to") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not to or not text:
        raise HTTPException(status_code=422, detail="Se requieren 'to' y 'text'")

    if not await _send(to, text, role="human"):
        raise HTTPException(
            status_code=502, detail="No se pudo enviar. Revisa el canal y las credenciales."
        )
    return {"sent": True, "to": to}


@app.post("/admin/simulate", dependencies=[Depends(require_admin)])
async def simulate(payload: dict[str, Any]) -> dict[str, Any]:
    """Prueba el prompt contra la base de conocimiento sin enviar nada."""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Se requiere 'text'")

    history: list[dict[str, str]] = []
    for turn in payload.get("history") or []:
        if isinstance(turn, dict) and turn.get("content"):
            history.append(
                {
                    "role": "user" if turn.get("role") == "user" else "assistant",
                    "content": str(turn["content"]),
                }
            )
    history.append({"role": "user", "content": text})

    answer = await get_agent().answer(
        history=history,
        knowledge=get_kb().text(),
        business_name=settings.business_name,
    )
    body = answer.reply.strip()
    if answer.handoff:
        body = f"{body}\n\n{settings.handoff_message}".strip()

    return {
        "reply": body,
        "reply_whatsapp": to_whatsapp(body),
        "reply_telegram": to_telegram(body),
        "handoff": answer.handoff,
        "handoff_reason": answer.handoff_reason,
    }


@app.get("/admin/knowledge", dependencies=[Depends(require_admin)])
async def knowledge_info() -> JSONResponse:
    kb = get_kb()
    text = kb.text()
    return JSONResponse(
        {
            "path": str(kb.path),
            "characters": len(text),
            "loaded": bool(text),
            "preview": text[:400],
        }
    )
