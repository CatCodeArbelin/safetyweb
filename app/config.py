"""Application configuration."""

import json
from typing import Annotated, Literal
from urllib.parse import quote_plus

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class XuiNodeConfig(BaseModel):
    """Configuration for a single X-UI node."""

    model_config = ConfigDict(populate_by_name=True)

    key: str
    name: str | None = None
    enabled: bool = True
    max_active_subscriptions: int | None = None
    weight: int = 1
    default_traffic_gb: int | None = None
    default_limit_ip: int | None = None
    xui_base_url: str = Field(
        default="http://localhost:2053",
        validation_alias=AliasChoices("base_url", "xui_base_url"),
    )
    xui_public_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("public_host", "xui_public_host"),
    )
    xui_sub_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sub_base_url", "xui_sub_base_url"),
    )
    xui_api_token: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("api_token", "xui_api_token"),
    )
    xui_auth_mode: Literal["session_cookie", "api_token"] = Field(
        default="session_cookie",
        validation_alias=AliasChoices("auth_mode", "xui_auth_mode"),
    )
    xui_username: str = Field(
        validation_alias=AliasChoices("username", "xui_username"),
    )
    xui_password: SecretStr = Field(
        repr=False,
        validation_alias=AliasChoices("password", "xui_password"),
    )
    xui_inbound_ids: Annotated[list[int], NoDecode] = Field(
        validation_alias=AliasChoices("inbound_ids", "xui_inbound_ids"),
    )

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Validate node key is not empty."""
        normalized = value.strip()
        if not normalized:
            msg = "X-UI node key must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        """Validate node display name is not empty when configured."""
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            msg = "X-UI node name must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("max_active_subscriptions")
    @classmethod
    def validate_max_active_subscriptions(cls, value: int | None) -> int | None:
        """Validate node capacity limit is positive when configured."""
        if value is not None and value <= 0:
            msg = "max_active_subscriptions must be positive"
            raise ValueError(msg)
        return value

    @field_validator("default_traffic_gb", "default_limit_ip")
    @classmethod
    def validate_optional_non_negative_int(cls, value: int | None) -> int | None:
        """Validate optional per-node provisioning defaults are not negative."""
        if value is not None and value < 0:
            msg = "node provisioning defaults must not be negative"
            raise ValueError(msg)
        return value

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, value: int) -> int:
        """Validate node selection weight is positive."""
        if value <= 0:
            msg = "weight must be positive"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def set_default_name(self) -> "XuiNodeConfig":
        """Use the node key as the display name when no name is configured."""
        if self.name is None:
            self.name = self.key
        return self

    @field_validator("xui_inbound_ids", mode="before")
    @classmethod
    def parse_xui_inbound_ids(
        cls,
        value: str | list[int] | list[str] | None,
    ) -> list[int]:
        """Parse and validate node inbound IDs."""
        inbound_ids = Settings._parse_int_list(value, "xui_inbound_ids")
        if not inbound_ids:
            msg = "xui_inbound_ids must not be empty"
            raise ValueError(msg)
        return inbound_ids


class PlategaPaymentMethodConfig(BaseModel):
    """Configuration for one selectable Platega payment method."""

    title: str
    payment_method: str

    @field_validator("title", "payment_method")
    @classmethod
    def validate_not_empty(cls, value: str) -> str:
        """Validate display title and Platega payment method are not empty."""
        normalized = value.strip()
        if not normalized:
            msg = "Platega payment method title and payment_method must not be empty"
            raise ValueError(msg)
        return normalized


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
    xui_api_token: SecretStr | None = Field(default=None, repr=False)
    xui_auth_mode: Literal["session_cookie", "api_token"] = "session_cookie"
    xui_username: str | None = None
    xui_password: SecretStr | None = Field(default=None, repr=False)
    xui_inbound_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    xui_nodes_json: Annotated[list[XuiNodeConfig], NoDecode] = Field(
        default_factory=list,
        repr=False,
    )
    xui_expired_client_policy: str = "disable"
    xui_default_traffic_gb: int = 0
    xui_default_limit_ip: int = 1
    test_mode: bool = False
    test_mode_referral_rewards_enabled: bool = False
    trial_access_enabled: bool = True
    trial_access_hours: int = 2
    referral_enabled: bool = True
    referral_new_user_bonus_days: int = 3
    referral_month_1_bonus_days: int = 3
    referral_month_3_bonus_days: int = 7
    referral_month_6_bonus_days: int = 14
    referral_month_12_bonus_days: int = 21
    referral_max_bonus_days_per_month: int = 30
    early_buyer_discount_enabled: bool = True
    early_buyer_limit: int = 100
    early_buyer_discount_percent: int = 15
    service_name: str = "ЛадНет"
    service_display_name: str = "🌏 ЛадНет | Безопасный Интернет"
    bot_public_url: str = "https://t.me/LadnetBot"
    support_username: str = "@arbelin94"
    support_second_username: str | None = "@BamboleiloO87"
    support_email: str | None = "catcodework@gmail.com"
    privacy_policy_url: str = (
        "https://telegra.ph/Politika-konfidencialnosti-LadNet-06-16"
    )
    terms_url: str = "https://telegra.ph/Polzovatelskoe-soglashenie-LadNet-06-16"
    tariffs_url: str = "https://telegra.ph/Tarify-i-usloviya-oplaty-LadNet-06-16"
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    payment_provider: Literal["manual", "platega"] = "manual"
    app_http_host: str = "0.0.0.0"
    app_http_port: int = 8000
    platega_base_url: str = "https://app.platega.io"
    platega_merchant_id: str | None = None
    platega_api_key: SecretStr | None = None
    platega_callback_secret: SecretStr | None = None
    platega_payment_method: str | None = None
    platega_payment_methods_json: Annotated[
        dict[str, PlategaPaymentMethodConfig], NoDecode
    ] = Field(
        default_factory=dict,
        repr=False,
    )
    platega_return_url: str | None = None
    platega_failed_url: str | None = None
    platega_callback_path: str = "/payments/platega/callback"
    platega_test_mode: bool = False
    platega_reconcile_interval_seconds: int = 300
    platega_webhook_retry_interval_seconds: int = 60
    platega_webhook_max_attempts: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "platega_webhook_max_attempts",
            "platega_webhook_max_retries",
        ),
    )
    platega_webhook_retry_base_seconds: int = 30
    platega_webhook_retry_max_seconds: int = 900

    @field_validator("payment_provider", mode="before")
    @classmethod
    def normalize_payment_provider(cls, value: str) -> str:
        """Normalize selected payment provider."""
        return value.lower() if isinstance(value, str) else value

    @field_validator("platega_payment_method", mode="before")
    @classmethod
    def normalize_platega_payment_method(cls, value: str | None) -> str | None:
        """Normalize the legacy single Platega payment method setting."""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        """Validate cross-field settings and defaults."""
        self._validate_xui_nodes()

        if self.platega_callback_secret is None:
            self.platega_callback_secret = self.platega_api_key

        if self.payment_provider == "platega":
            missing_fields = [
                field_name
                for field_name in (
                    "platega_merchant_id",
                    "platega_api_key",
                    "platega_return_url",
                    "platega_failed_url",
                )
                if getattr(self, field_name) in (None, "")
            ]
            if missing_fields:
                msg = "Platega payment provider requires: " + ", ".join(missing_fields)
                raise ValueError(msg)

        return self

    def _validate_xui_nodes(self) -> None:
        """Validate X-UI node definitions and legacy fallback settings."""
        nodes = self.xui_nodes_json
        if nodes:
            keys = [node.key for node in nodes]
            if len(keys) != len(set(keys)):
                msg = "XUI_NODES_JSON contains duplicate node keys"
                raise ValueError(msg)
            return

        missing_fields = [
            field_name
            for field_name in ("xui_username", "xui_password")
            if getattr(self, field_name) in (None, "")
        ]
        if missing_fields:
            msg = "Legacy X-UI configuration requires: " + ", ".join(missing_fields)
            raise ValueError(msg)
        if not self.xui_inbound_ids:
            msg = "XUI_INBOUND_IDS must not be empty"
            raise ValueError(msg)

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

    @field_validator("xui_nodes_json", mode="before")
    @classmethod
    def parse_xui_nodes_json(
        cls,
        value: str | list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        """Parse XUI_NODES_JSON from a JSON array."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value
        if not isinstance(parsed, list):
            msg = "XUI_NODES_JSON must be a JSON array"
            raise TypeError(msg)
        return parsed

    @field_validator("platega_payment_methods_json", mode="before")
    @classmethod
    def parse_platega_payment_methods_json(
        cls,
        value: str | dict[str, dict[str, object]] | None,
    ) -> dict[str, dict[str, object]]:
        """Parse PLATEGA_PAYMENT_METHODS_JSON from a JSON object keyed by method code."""
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value
        if not isinstance(parsed, dict):
            msg = "PLATEGA_PAYMENT_METHODS_JSON must be a JSON object"
            raise TypeError(msg)
        normalized: dict[str, dict[str, object]] = {}
        for key, method in parsed.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                msg = "PLATEGA_PAYMENT_METHODS_JSON method codes must not be empty"
                raise ValueError(msg)
            if not isinstance(method, dict):
                msg = "PLATEGA_PAYMENT_METHODS_JSON method definitions must be objects"
                raise TypeError(msg)
            normalized[normalized_key] = method
        return normalized

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

    @property
    def xui_nodes(self) -> list[XuiNodeConfig]:
        """Return configured X-UI nodes or the legacy default node."""
        if self.xui_nodes_json:
            return self.xui_nodes_json

        if self.xui_username is None or self.xui_password is None:
            msg = "Legacy X-UI configuration is incomplete"
            raise ValueError(msg)

        return [
            XuiNodeConfig(
                key="default",
                name="Default",
                xui_base_url=self.xui_base_url,
                xui_public_host=self.xui_public_host,
                xui_sub_base_url=self.xui_sub_base_url,
                xui_api_token=self.xui_api_token,
                xui_auth_mode=self.xui_auth_mode,
                xui_username=self.xui_username,
                xui_password=self.xui_password,
                xui_inbound_ids=self.xui_inbound_ids,
            ),
        ]

    def get_platega_payment_method(self, key: str) -> PlategaPaymentMethodConfig:
        """Return a configured Platega payment method by UI key."""
        normalized = key.strip()
        if not normalized:
            msg = "Platega payment method code must not be empty"
            raise ValueError(msg)
        try:
            return self.platega_payment_methods_json[normalized]
        except KeyError as error:
            msg = f"Platega payment method code is not configured: {normalized}"
            raise KeyError(msg) from error

    def get_xui_node(self, key: str) -> XuiNodeConfig:
        """Return an X-UI node by key."""
        normalized = key.strip()
        if not normalized:
            msg = "X-UI node key must not be empty"
            raise ValueError(msg)

        for node in self.xui_nodes:
            if node.key == normalized:
                return node

        msg = f"X-UI node not found: {normalized}"
        raise KeyError(msg)
