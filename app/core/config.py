"""
Core configuration — loaded from environment / .env file.
All settings live here; import `settings` everywhere.
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: str = Field(default="development", alias="APP_ENV")
    app_secret_key: str = Field(default="dev-secret-change-in-production", alias="APP_SECRET_KEY")
    app_port: int = Field(default=8000, alias="APP_PORT")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_title: str = "Restaurant Growth OS"
    app_version: str = "2.0.0"

    # GoPOS
    gopos_client_id: str = Field(default="", alias="GOPOS_CLIENT_ID")
    gopos_client_secret: str = Field(default="", alias="GOPOS_CLIENT_SECRET")
    gopos_organization_id: str = Field(default="", alias="GOPOS_ORGANIZATION_ID")
    gopos_base_url: str = Field(default="https://app.gopos.io", alias="GOPOS_BASE_URL")
    gopos_read_only: bool = Field(default=True, alias="GOPOS_READ_ONLY")

    # LLM
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: Optional[str] = Field(default=None, alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    openai_vision_model: str = Field(default="grok-2-vision-1212", alias="OPENAI_VISION_MODEL")

    # Database
    duckdb_path: str = Field(default="data/gopos.duckdb", alias="DUCKDB_PATH")
    # PostgreSQL (Neon) — main multi-tenant store. Falls back to local SQLite for dev tests.
    database_url: str = Field(default="sqlite:///data/dev.db", alias="DATABASE_URL")

    # Security / auth
    jwt_secret: str = Field(default="dev-jwt-secret-change-me", alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = Field(default=720, alias="JWT_EXPIRE_HOURS")  # 30 days
    # Fernet key(s) for encrypting tenant secrets (GoPOS creds). Comma-separated for rotation.
    fernet_key: str = Field(default="", alias="FERNET_KEY")
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")  # True behind HTTPS

    # Sync
    sync_interval_minutes: int = Field(default=15, alias="SYNC_INTERVAL_MINUTES")

    # Research / web search (pluggable). Default: free DuckDuckGo (no key).
    # Set SEARCH_PROVIDER=brave|serper|tavily + SEARCH_API_KEY for higher quality.
    search_provider: str = Field(default="duckduckgo", alias="SEARCH_PROVIDER")
    search_api_key: Optional[str] = Field(default=None, alias="SEARCH_API_KEY")

    # Restaurant identity (used by CEO for web research context)
    restaurant_name: Optional[str] = Field(default=None, alias="RESTAURANT_NAME")

    # Optional integrations
    openweather_api_key: Optional[str] = Field(default=None, alias="OPENWEATHER_API_KEY")
    restaurant_city: Optional[str] = Field(default=None, alias="RESTAURANT_CITY")
    slack_webhook_url: Optional[str] = Field(default=None, alias="SLACK_WEBHOOK_URL")
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, alias="TELEGRAM_CHAT_ID")
    briefing_hour: int = Field(default=8, alias="BRIEFING_HOUR")  # local server time

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_webhook_url)

    @property
    def notifications_configured(self) -> bool:
        return self.telegram_configured or self.slack_configured

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def gopos_configured(self) -> bool:
        return bool(self.gopos_client_id and self.gopos_client_secret and self.gopos_organization_id)

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# Convenient singleton import: `from app.core.config import settings`
settings = get_settings()
