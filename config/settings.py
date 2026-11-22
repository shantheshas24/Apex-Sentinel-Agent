"""
Central configuration loaded from .env via pydantic-settings.
All modules import get_settings() to access config — never use os.environ directly.
"""
from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── OpenRouter LLM ────────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # ── HuggingFace (optional — higher rate limits for embedding model downloads)
    huggingface_token: str = ""

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    chroma_persist_path: str = "./chroma_data"
    chroma_embedding_model: str = "all-MiniLM-L6-v2"

    # ── MCP Server ────────────────────────────────────────────────────────────
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8000

    # ── FastAPI Dashboard ─────────────────────────────────────────────────────
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080

    # ── Google OAuth ──────────────────────────────────────────────────────────
    google_credentials_path: str = "./secrets/credentials.json"
    google_token_path: str = "./secrets/token.json"

    # ── Twilio ────────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_to_number: str = ""

    # ── Observer ──────────────────────────────────────────────────────────────
    observer_poll_interval: int = 60

    # Deadline reminders
    deadline_timezone: str = "UTC"
    deadline_quiet_start: time = time(22)
    deadline_quiet_end: time = time(7)
    deadline_reminder_interval: int = Field(default=15, ge=1)
    deadline_hourly_limit: int = Field(default=5, ge=1)
    deadline_phone_daily_limit: int = Field(default=2, ge=1)
    deadline_phone_enabled: bool = False

    @field_validator("deadline_timezone")
    @classmethod
    def validate_deadline_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Use an IANA time zone such as UTC or Asia/Kolkata") from exc
        return value

    @property
    def deadline_phone_available(self) -> bool:
        return bool(
            self.deadline_phone_enabled and self.twilio_enabled
            and self.twilio_from_number and self.twilio_to_number
        )

    @property
    def mcp_sse_url(self) -> str:
        return f"http://{self.mcp_server_host}:{self.mcp_server_port}/sse"

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
