"""Agente de soporte sobre Gemini con salida estructurada.

El modelo devuelve JSON con tres campos: el mensaje para el cliente, si hace
falta pasar a un humano y el motivo. Así el escalado es determinista y no
depende de "adivinar" a partir del texto.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "Perdona, estoy teniendo un problema técnico. "
    "Ya avisé al equipo y te contactamos en un momento 🙏"
)

_DIAS = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]
_MESES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]


class SupportAnswer(BaseModel):
    """Respuesta estructurada del modelo."""

    reply: str = Field(
        description=(
            "Mensaje final para el cliente, en español, breve, cálido y listo "
            "para enviarse por WhatsApp."
        )
    )
    handoff: bool = Field(
        description="true si una persona del equipo debe continuar la conversación."
    )
    handoff_reason: str | None = Field(
        default=None,
        description="Motivo breve del escalado, en una frase corta (interno).",
    )


def human_datetime(timezone: str) -> str:
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # zona horaria inválida o tzdata ausente
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return (
        f"{_DIAS[now.weekday()]} {now.day} de {_MESES[now.month - 1]} "
        f"de {now.year}, {now:%H:%M}"
    )


SYSTEM_PROMPT = """Eres el asistente de atención al cliente de {business} por WhatsApp.

FECHA Y HORA ACTUAL: {today}

=== INFORMACIÓN DEL NEGOCIO (única fuente de verdad) ===
{knowledge}
=== FIN DE LA INFORMACIÓN ===

CÓMO RESPONDER
- Español neutro y cálido. Tutea al cliente. 1 a 4 líneas (máximo ~60 palabras).
- Responde la pregunta primero; no repitas el saludo en cada mensaje.
- Usa SOLO la información de arriba. Si un dato no aparece, no lo inventes.
- Formato WhatsApp: *negrita* para lo clave. Sin encabezados, tablas ni markdown complejo.
- Máximo una pregunta por mensaje.

CUÁNDO ESCALAR (handoff = true)
- El cliente pide hablar con una persona o "con un humano".
- La respuesta no está en la información del negocio.
- Reclamos, pedidos incompletos, errores de cobro, devoluciones o clientes molestos.
- El cliente insiste por tercera vez en algo que no puedes resolver.
- Temas legales, médicos, de seguridad o que requieran datos personales.
- Al escalar, el mensaje al cliente debe ser tranquilizador y sin prometer tiempos exactos.

PROHIBIDO
- Inventar precios, stock, horarios, promociones o plazos.
- Compartir datos de otros clientes o información interna.
- Pedir contraseñas, datos de tarjeta o documentos por chat.
- Confirmar reembolsos o descuentos que no estén en la información.
"""


def build_system_prompt(business: str, knowledge: str, today: str) -> str:
    if not knowledge.strip():
        knowledge = (
            "(No hay información cargada del negocio. No inventes nada: "
            "responde solo saludos y escalado a un humano.)"
        )
    return SYSTEM_PROMPT.format(business=business, knowledge=knowledge.strip(), today=today)


class SupportAgent:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.5,
        max_output_tokens: int = 600,
        thinking_budget: int | None = 0,
        timezone: str = "America/Mexico_City",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._thinking_budget = thinking_budget
        self._timezone = timezone
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def answer(
        self, *, history: list[dict[str, str]], knowledge: str, business_name: str
    ) -> SupportAnswer:
        if not self._api_key:
            log.error("GEMINI_API_KEY no configurada.")
            return SupportAnswer(
                reply=FALLBACK_REPLY, handoff=True, handoff_reason="IA sin configurar"
            )

        contents = self._build_contents(history)
        if not contents:
            return SupportAnswer(reply="¿En qué te puedo ayudar?", handoff=False)

        config = self._build_config(business_name, knowledge)

        try:
            response = await self.client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except Exception:
            log.exception("Error llamando a Gemini")
            return SupportAnswer(
                reply=FALLBACK_REPLY, handoff=True, handoff_reason="error de la IA"
            )

        return self._parse(response)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_contents(history: list[dict[str, str]]) -> list[types.Content]:
        contents: list[types.Content] = []
        for message in history:
            text = (message.get("content") or "").strip()
            if not text:
                continue
            role = "user" if message.get("role") == "user" else "model"
            # Gemini espera roles alternados: fusionamos mensajes consecutivos.
            if contents and contents[-1].role == role:
                contents[-1].parts.append(types.Part(text=text))
            else:
                contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
        return contents

    def _build_config(self, business_name: str, knowledge: str) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "system_instruction": build_system_prompt(
                business_name, knowledge, human_datetime(self._timezone)
            ),
            "temperature": self._temperature,
            "max_output_tokens": self._max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": SupportAnswer,
        }

        thinking_config = getattr(types, "ThinkingConfig", None)
        if self._thinking_budget is not None and thinking_config is not None:
            try:
                kwargs["thinking_config"] = thinking_config(
                    thinking_budget=self._thinking_budget
                )
            except Exception:  # versión del SDK sin soporte
                log.debug("ThinkingConfig no soportado por esta versión del SDK")

        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _parse(response: Any) -> SupportAnswer:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, SupportAnswer) and parsed.reply.strip():
            return parsed
        if isinstance(parsed, BaseModel):
            data = parsed.model_dump()
            if str(data.get("reply", "")).strip():
                return SupportAnswer(**data)

        raw = (getattr(response, "text", "") or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                answer = SupportAnswer(**data)
                if answer.reply.strip():
                    return answer
            except Exception:
                log.debug("Respuesta no estructurada: %s", raw[:200])
                return SupportAnswer(reply=raw, handoff=False)

        return SupportAnswer(
            reply=FALLBACK_REPLY, handoff=True, handoff_reason="respuesta vacía de la IA"
        )
