"""Configuración central.

Todos los valores se leen de variables de entorno o del archivo `.env`
que está en la raíz del proyecto.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- WhatsApp Cloud API ----------
    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_verify_token: str = "cambia-este-token"
    wa_app_secret: str = ""
    wa_api_version: str = "v21.0"
    wa_mark_as_read: bool = True

    # ---------- Telegram (para pruebas y como canal alternativo) ----------
    telegram_bot_token: str = ""
    # "polling" = el servidor pregunta a Telegram (no necesitas URL pública).
    # "webhook" = Telegram llama a /telegram/webhook (necesita URL HTTPS).
    # "off" = desactivado.
    telegram_mode: str = "polling"
    telegram_webhook_secret: str = ""

    # ---------- Gemini ----------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_temperature: float = 0.5
    gemini_max_output_tokens: int = 600
    gemini_thinking_budget: int | None = 0

    # ---------- Comportamiento ----------
    business_name: str = "Mi Negocio"
    history_turns: int = 12
    buffer_seconds: float = 2.0
    handoff_message: str = (
        "Te paso con una persona del equipo para ayudarte mejor. "
        "En breve te escribimos por aquí 🙌"
    )
    handoff_webhook_url: str = ""
    timezone: str = "America/Mexico_City"

    # ---------- Infraestructura ----------
    knowledge_path: str = "knowledge/negocio.md"
    database_path: str = "data/bot.db"
    admin_key: str = "cambia-esta-clave"
    log_level: str = "INFO"

    @field_validator("gemini_thinking_budget", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # Permite dejar la variable vacía en el .env sin romper la validación.
        return None if value == "" else value

    def _absolute(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def knowledge_file(self) -> Path:
        return self._absolute(self.knowledge_path)

    @property
    def database_file(self) -> Path:
        return self._absolute(self.database_path)

    def active_channels(self) -> list[str]:
        """Canales con credenciales completas."""
        channels = []
        if self.wa_token and self.wa_phone_number_id:
            channels.append("whatsapp")
        if self.telegram_bot_token and self.telegram_mode != "off":
            channels.append("telegram")
        return channels

    def missing_required(self) -> list[str]:
        """Variables sin las que el bot no puede funcionar."""
        missing: list[str] = []
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not self.active_channels():
            missing.append("WA_TOKEN+WA_PHONE_NUMBER_ID o TELEGRAM_BOT_TOKEN")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
