"""Application configuration."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/safetyweb"
    redis_url: str = "redis://localhost:6379/0"
    xui_base_url: str = "http://localhost:2053"
    xui_username: str = ""
    xui_password: str = ""
