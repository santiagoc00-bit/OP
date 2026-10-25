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
        fallback_model: str | None = "gemini-flash-latest",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._thinking_budget = thinking_budget
        self._timezone = timezone
        self._fallback_model = fallback_model
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

        try:
            response = await self._generate(contents, business_name, knowledge)
        except Exception as exc:
            log.error("Gemini fallo (modelo '%s'): %s: %s", self._model, type(exc).__name__, exc)
            log.debug("Detalle del error de Gemini", exc_info=True)
            if self._is_missing_model(exc):
                log.error(
                    "Ese modelo no existe o no esta disponible para tu clave. "
                    "Ejecuta check.cmd o cambia GEMINI_MODEL en .env."
                )
            return SupportAnswer(
                reply=FALLBACK_REPLY,
                handoff=True,
                handoff_reason=f"error de la IA ({type(exc).__name__})",
            )

        return self._parse(response)

    async def _generate(
        self, contents: list[types.Content], business_name: str, knowledge: str
    ) -> Any:
        """Llama a Gemini probando configuraciones cada vez más simples.

        No todos los modelos aceptan `response_schema` o `thinking_budget`, y una
        respuesta vacía tampoco sirve. En vez de fallar, se degrada:
          1. JSON estructurado (lo ideal)
          2. JSON estructurado sin "thinking"
          3. Texto plano pidiendo JSON en el prompt
        """
        configs = self._configs(business_name, knowledge)

        try:
            return await self._try_configs(self._model, contents, configs)
        except Exception as exc:
            fallback = self._fallback_model
            if (
                not fallback
                or fallback == self._model
                or not self._is_missing_model(exc)
            ):
                raise
            log.warning(
                "El modelo '%s' no esta disponible; probando con '%s'. "
                "Actualiza GEMINI_MODEL en .env.",
                self._model,
                fallback,
            )
            return await self._try_configs(fallback, contents, configs)

    async def _try_configs(
        self,
        model: str,
        contents: list[types.Content],
        configs: list[types.GenerateContentConfig],
    ) -> Any:
        last_error: Exception | None = None
        for index, config in enumerate(configs, start=1):
            try:
                return await self.client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:
                last_error = exc
                if index < len(configs):
                    log.warning(
                        "Gemini rechazo la configuracion %s/%s (%s: %s). "
                        "Reintentando de forma mas simple.",
                        index,
                        len(configs),
                        type(exc).__name__,
                        str(exc)[:200],
                    )
        assert last_error is not None
        raise last_error

    @staticmethod
    def _is_missing_model(exc: Exception) -> bool:
        text = str(exc).upper()
        return "NOT_FOUND" in text or "404" in text

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

    def _configs(self, business_name: str, knowledge: str) -> list[types.GenerateContentConfig]:
        """Configuraciones ordenadas de más a menos estricta."""
        system = build_system_prompt(
            business_name, knowledge, human_datetime(self._timezone)
        )
        base: dict[str, Any] = {
            "system_instruction": system,
            "temperature": self._temperature,
            "max_output_tokens": self._max_output_tokens,
        }
        structured: dict[str, Any] = {
            **base,
            "response_mime_type": "application/json",
            "response_schema": SupportAnswer,
        }

        configs: list[types.GenerateContentConfig] = []

        thinking_cls = getattr(types, "ThinkingConfig", None)
        if self._thinking_budget is not None and thinking_cls is not None:
            try:
                thinking = thinking_cls(thinking_budget=self._thinking_budget)
            except Exception:  # versión del SDK sin soporte
                log.debug("ThinkingConfig no soportado por esta versión del SDK")
            else:
                configs.append(
                    types.GenerateContentConfig(**structured, thinking_config=thinking)
                )

        configs.append(types.GenerateContentConfig(**structured))

        # Último recurso: texto plano pidiendo el JSON dentro del prompt.
        plain = dict(base)
        plain["system_instruction"] = (
            system
            + "\n\nFORMATO OBLIGATORIO DE SALIDA: responde ÚNICAMENTE con un objeto JSON "
            'con las claves "reply" (texto para el cliente), "handoff" (true o false) '
            'y "handoff_reason" (texto o null). Sin markdown ni explicaciones.'
        )
        configs.append(types.GenerateContentConfig(**plain))

        return configs

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
