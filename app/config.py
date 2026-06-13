"""Application configuration."""

from typing import Annotated
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: SecretStr
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "safetyweb"
    postgres_user: str = "postgres"
    postgres_password: SecretStr
    redis_url: str = "redis://localhost:6379/0"
    xui_base_url: str = "http://localhost:2053"
    xui_username: str
    xui_password: SecretStr
    xui_inbound_id: int
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: str | list[int] | list[str] | None) -> list[int]:
        """Parse ADMIN_IDS from a comma-separated string or a list."""
        if value is None or value == "":
            return []

        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]

        if isinstance(value, list):
            return [int(item) for item in value]

        msg = "ADMIN_IDS must be a comma-separated string or a list of integers"
        raise TypeError(msg)

    @property
    def database_url(self) -> str:
        """Build the async PostgreSQL DSN from individual environment settings."""
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password.get_secret_value())
        host = self.postgres_host
        port = self.postgres_port
        database = quote_plus(self.postgres_db)
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
