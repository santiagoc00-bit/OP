"""Diagnostico del asistente.

Uso:
    .\\check.cmd
    .\\.venv\\Scripts\\python.exe check.py

Comprueba la configuracion, el token de Telegram, la clave de Gemini (y que el
modelo configurado exista de verdad) y la base de conocimiento.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from app.config import BASE_DIR, settings
from app.telegram import TelegramClient, TelegramError

OK = "[ OK  ]"
BAD = "[FALLA]"
WARN = "[AVISO]"

problems = 0
warnings = 0


def safe(value: Any) -> str:
    """Evita UnicodeEncodeError en consolas Windows antiguas."""
    text = str(value)
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "replace").decode("ascii")


def line(prefix: str, message: str) -> None:
    print(f"{prefix} {safe(message)}")


def info(message: str) -> None:
    print(f"        {safe(message)}")


def ok(message: str) -> None:
    line(OK, message)


def bad(message: str) -> None:
    global problems
    problems += 1
    line(BAD, message)


def warn(message: str) -> None:
    global warnings
    warnings += 1
    line(WARN, message)


def mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "(vacio)"
    if len(value) <= 10:
        return value[0] + "*" * (len(value) - 1)
    return f"{value[:6]}...{value[-4:]}"


def section(title: str) -> None:
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------- #
def check_config() -> None:
    section("1. Configuracion (.env)")

    env_file = BASE_DIR / ".env"
    if env_file.exists():
        ok(f".env encontrado ({env_file})")
    else:
        bad("No existe .env. Copia .env.example como .env.")

    info(f"GEMINI_API_KEY      = {mask(settings.gemini_api_key)}")
    info(f"GEMINI_MODEL        = {settings.gemini_model}")
    info(f"TELEGRAM_BOT_TOKEN  = {mask(settings.telegram_bot_token)} (modo {settings.telegram_mode})")
    info(f"WA_TOKEN            = {mask(settings.wa_token)}")
    info(f"WA_PHONE_NUMBER_ID  = {mask(settings.wa_phone_number_id)}")

    if not settings.gemini_api_key:
        bad("Falta GEMINI_API_KEY: el bot nunca podra responder con IA.")
    if not settings.active_channels():
        bad("Ningun canal completo: falta TELEGRAM_BOT_TOKEN o WA_TOKEN+WA_PHONE_NUMBER_ID.")

    # Variables de entorno que podrian desviar las llamadas del SDK.
    suspicious = {
        key: value
        for key, value in os.environ.items()
        if value
        and any(token in key.upper() for token in ("GENAI", "GOOGLE_", "GEMINI_"))
        and key.upper() not in {"GEMINI_API_KEY"}
    }
    if suspicious:
        warn("Variables de entorno globales que pueden afectar al SDK:")
        for key, value in suspicious.items():
            info(f"{key} = {mask(value)}")


def check_knowledge() -> None:
    section("2. Base de conocimiento")

    path = settings.knowledge_file
    if not path.exists():
        bad(f"No existe {path}. El bot escalara todo a un humano.")
        return

    text = path.read_text(encoding="utf-8")
    if len(text) < 200:
        warn(f"Solo {len(text)} caracteres: el bot tendra poco contexto.")
    else:
        ok(f"{path.name}: {len(text)} caracteres")

    if "EJEMPLO" in text and "Cafe Luna" in text:
        warn("Sigue siendo el ejemplo de la cafeteria: reemplazalo por tu negocio.")


def check_telegram() -> None:
    section("3. Telegram")

    if not settings.telegram_bot_token:
        warn("Sin TELEGRAM_BOT_TOKEN: Telegram desactivado.")
        return

    async def get_me() -> dict:
        client = TelegramClient(settings.telegram_bot_token, timeout=15)
        try:
            return await client.get_me()
        finally:
            await client.aclose()

    try:
        me = asyncio.run(get_me())
    except TelegramError as exc:
        bad(f"El token de Telegram no funciona: {exc}")
        return
    except Exception as exc:  # red, DNS...
        bad(f"No se pudo contactar con Telegram: {type(exc).__name__}: {exc}")
        return

    ok(f"Telegram conectado como @{me.get('username')} ({me.get('first_name')})")

    if settings.telegram_mode == "webhook":
        info("Modo webhook: recuerda registrar la URL con setWebhook.")
    elif settings.telegram_mode == "polling":
        info("Modo polling: no necesitas URL publica.")
    else:
        bad(f"TELEGRAM_MODE='{settings.telegram_mode}' no es valido (polling|webhook|off).")


def check_gemini() -> None:
    section("4. Gemini")

    if not settings.gemini_api_key:
        bad("Sin GEMINI_API_KEY: se omite la prueba.")
        return

    try:
        from google import genai
    except ImportError:
        bad("Falta el paquete google-genai. Ejecuta: pip install -r requirements.txt")
        return

    client = genai.Client(api_key=settings.gemini_api_key)

    # 1) Listar los modelos que la clave puede usar.
    models: list[str] = []
    try:
        for model in client.models.list():
            actions = model.supported_actions or []
            if "generateContent" in actions and "gemini" in (model.name or ""):
                models.append((model.name or "").removeprefix("models/"))
            if len(models) >= 40:
                break
    except Exception as exc:
        bad(f"No se pudieron listar los modelos: {type(exc).__name__}: {exc}")
        info("Suele ser una API key invalida, caducada o de otro proyecto.")
        return

    if models:
        ok(f"La clave funciona. {len(models)} modelos disponibles para generateContent.")
        info("Algunos: " + ", ".join(models[:8]))
    else:
        warn("La clave responde pero no hay modelos gemini con generateContent.")

    configured = settings.gemini_model.removeprefix("models/")
    if models and configured not in models:
        warn(f"'{configured}' no esta en la lista de tu cuenta (¿modelo retirado?).")

    # 2) Probar exactamente la llamada que hace el bot (agente completo).
    try:
        from app.ai import FALLBACK_REPLY, SupportAgent
    except ImportError:
        bad("No se pudo importar app.ai (dependencias incompletas).")
        return

    knowledge = ""
    if settings.knowledge_file.exists():
        knowledge = settings.knowledge_file.read_text(encoding="utf-8")

    agent = SupportAgent(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        temperature=settings.gemini_temperature,
        max_output_tokens=settings.gemini_max_output_tokens,
        thinking_budget=settings.gemini_thinking_budget,
        timezone=settings.timezone,
        fallback_model=settings.gemini_fallback_model,
    )

    try:
        answer = asyncio.run(
            agent.answer(
                history=[{"role": "user", "content": "Hola, ¿abren hoy?"}],
                knowledge=knowledge,
                business_name=settings.business_name,
            )
        )
    except Exception as exc:
        bad(f"El agente fallo: {type(exc).__name__}: {exc}")
        return

    if answer.reply.strip() == FALLBACK_REPLY:
        bad("El agente devolvio el mensaje de emergencia: la IA no funciona.")
        info("La terminal de run.cmd muestra la linea 'Gemini fallo' con la causa.")
        return

    modo = "escala a humano" if answer.handoff else "responde solo"
    ok(f"Agente OK ({modo}): {answer.reply[:80]!r}")
    if answer.handoff:
        info(f"Motivo del escalado: {answer.handoff_reason}")


# --------------------------------------------------------------------------- #
def main() -> int:
    print("Diagnostico del asistente de WhatsApp/Telegram")
    print("=" * 52)

    check_config()
    check_knowledge()
    check_telegram()
    check_gemini()

    print("\n" + "=" * 52)
    if problems:
        print(f"RESULTADO: {problems} problema(s), {warnings} aviso(s).")
        print("El bot no respondera bien hasta que se corrija lo marcado con [FALLA].")
        return 1

    if warnings:
        print(f"RESULTADO: todo funciona, con {warnings} aviso(s).")
    else:
        print("RESULTADO: todo correcto.")
    print("Arranca el bot con:  run.cmd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
