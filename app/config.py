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
    xui_public_host: str | None = None
    xui_sub_base_url: str | None = None
    xui_api_token: SecretStr | None = None
    xui_username: str
    xui_password: SecretStr
    xui_inbound_ids: Annotated[list[int], NoDecode]
    xui_expired_client_policy: str = "disable"
    xui_default_traffic_gb: int = 0
    xui_default_limit_ip: int = 1
    test_mode: bool = False
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    @field_validator("xui_expired_client_policy")
    @classmethod
    def validate_xui_expired_client_policy(cls, value: str) -> str:
        """Validate how expired X-UI clients are deprovisioned."""
        normalized = value.lower()
        if normalized not in {"disable", "delete"}:
            msg = "XUI_EXPIRED_CLIENT_POLICY must be either 'disable' or 'delete'"
            raise ValueError(msg)
        return normalized

    @field_validator("xui_inbound_ids", mode="before")
    @classmethod
    def parse_xui_inbound_ids(
        cls,
        value: str | list[int] | list[str] | None,
    ) -> list[int]:
        """Parse XUI_INBOUND_IDS from a comma-separated string or a list."""
        return cls._parse_int_list(value, "XUI_INBOUND_IDS")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: str | list[int] | list[str] | None) -> list[int]:
        """Parse ADMIN_IDS from a comma-separated string or a list."""
        return cls._parse_int_list(value, "ADMIN_IDS")

    @staticmethod
    def _parse_int_list(
        value: str | list[int] | list[str] | None,
        env_name: str,
    ) -> list[int]:
        """Parse comma-separated strings or lists into a list of integers."""
        if value is None or value == "":
            return []

        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]

        if isinstance(value, list):
            return [int(item) for item in value]

        msg = f"{env_name} must be a comma-separated string or a list of integers"
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
