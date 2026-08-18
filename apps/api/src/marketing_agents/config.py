"""Validated application settings with safe local defaults."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from marketing_agents.domain.retention import RetentionPolicy
from marketing_agents.security.network_policy import AdapterNetworkPolicy, NetworkPolicyError
from marketing_agents.security.secret_config import redact_config


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="forbid",
        frozen=True,
        case_sensitive=False,
    )

    app_env: Literal["local", "test", "production"] = "local"
    auth_mode: Literal["local"] = "local"
    database_url: str = "sqlite+aiosqlite:///./data/marketing_agents.db"
    catalog_root: Path = Path("catalog/v1")
    llm_provider: str = "mock"
    connector_mode: str = "mock"
    allow_external_network: bool = False
    real_llm_opt_in: bool = False
    real_connector_opt_in: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    marketing_agents_digest_key_path: Path = Path("data/digest.key")
    real_llm_api_key: SecretStr | None = None
    retention_admitted_payload_days: int = Field(default=7, ge=1, le=3650)
    retention_external_action_payload_days: int = Field(default=7, ge=1, le=3650)
    retention_approval_detail_days: int = Field(default=7, ge=1, le=3650)
    retention_artifact_detail_days: int = Field(default=30, ge=1, le=3650)
    retention_connector_receipt_detail_days: int = Field(default=30, ge=1, le=3650)
    retention_audit_metadata_days: int = Field(default=90, ge=1, le=3650)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        allowed = ("sqlite+aiosqlite://", "postgresql+asyncpg://")
        if not value.startswith(allowed):
            raise ValueError("database URL must use sqlite+aiosqlite or postgresql+asyncpg")
        return value

    @field_validator("api_host")
    @classmethod
    def validate_api_host(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("API host must be a loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("API host must remain loopback-bound in the local identity mode")
        return value

    @model_validator(mode="after")
    def validate_security_modes(self) -> Self:
        if self.app_env == "production" and self.auth_mode == "local":
            raise ValueError("production cannot use the local identity provider")
        try:
            AdapterNetworkPolicy(
                llm_provider=self.llm_provider,
                connector_mode=self.connector_mode,
                allow_external_network=self.allow_external_network,
                real_llm_opt_in=self.real_llm_opt_in,
                real_connector_opt_in=self.real_connector_opt_in,
            ).validate()
        except NetworkPolicyError as exc:
            raise ValueError(str(exc)) from exc
        if self.llm_provider != "mock" and self.real_llm_api_key is None:
            raise ValueError("real LLM mode requires a provider credential")
        return self

    @property
    def trusted_hosts(self) -> tuple[str, ...]:
        return ("127.0.0.1", "localhost", "testserver", "[::1]")

    def safe_snapshot(self) -> dict[str, object]:
        return cast(dict[str, object], redact_config(self.model_dump(mode="json")))

    @property
    def retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            admitted_payload_days=self.retention_admitted_payload_days,
            external_action_payload_days=self.retention_external_action_payload_days,
            approval_detail_days=self.retention_approval_detail_days,
            artifact_detail_days=self.retention_artifact_detail_days,
            connector_receipt_detail_days=self.retention_connector_receipt_detail_days,
            audit_metadata_days=self.retention_audit_metadata_days,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
