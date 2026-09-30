"""Explicit local configuration. Credentials are environment references, never values."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/AgentFlow"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AgentFlow"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agentflow"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    agent_concurrency: int = Field(default=3, ge=1, le=32)
    auto_review_repair_limit: int = Field(default=100, ge=-1, strict=True)
    auto_test_repair_limit: int = Field(default=100, ge=-1, strict=True)
    auto_failure_retry_limit: int = Field(default=100, ge=0, le=100)
    auto_timeout_retry_limit: int = Field(default=100, ge=0, le=100, strict=True)
    auto_failure_run_limit: int = Field(default=100, ge=0, le=100)
    auto_failure_retry_delay_seconds: int = Field(default=30, ge=0, le=3600)
    max_coding_steps: int = Field(default=32, ge=1, le=128)
    max_role_iterations: int = Field(default=1000, ge=1, strict=True)
    agent_max_log_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, strict=True)
    agent_startup_timeout_seconds: float = Field(default=60, gt=0, le=60)
    controller_startup_timeout_seconds: float = Field(default=120, ge=0, strict=True, allow_inf_nan=False)
    isolation_probe_timeout_seconds: float = Field(default=30, gt=0, le=120)
    package_fetch_timeout_seconds: float = Field(default=30, ge=1, le=3600)
    max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    owner_token_seconds: int = Field(default=8 * 3600, ge=60, le=86400)
    bootstrap_seconds: int = Field(default=300, ge=30, le=900)
    executor_host: str | None = None
    executor_port: int = Field(default=9443, ge=1024, le=65535)
    executor_origin: str | None = None
    tls_certificate: Path | None = None
    tls_private_key: Path | None = None
    tls_client_ca: Path | None = None
    trusted_project_execution: bool = False
    research_public_web_enabled: bool = Field(default=True, strict=True)
    research_web_hosts: list[str] = Field(default_factory=list, max_length=32)
    node_output_limit_bytes: int = Field(default=256 * 1024 * 1024, ge=1024 * 1024, le=256 * 1024 * 1024)
    node_active_seconds: int = Field(default=1800, ge=30, le=86400)
    package_cache_max_bytes: int = Field(default=256 * 1024 * 1024, ge=0, strict=True)
    dashboard_dir: Path | None = None

    @field_validator("research_web_hosts")
    @classmethod
    def literal_web_hosts(cls, value: list[str]) -> list[str]:
        import re
        if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
               or ".." in host or "." not in host for host in value):
            raise ValueError("Research hosts must be literal DNS hostnames without URLs or wildcards")
        return sorted({host.lower() for host in value})

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if not ipaddress.ip_address(value).is_loopback:
            raise ValueError("The owner listener must bind an explicit loopback address")
        return value

    @field_validator("data_dir")
    @classmethod
    def absolute_data_dir(cls, value: Path) -> Path:
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError("data_dir must be absolute")
        return value.resolve()

    @model_validator(mode="after")
    def executor_tls(self) -> Settings:
        if self.executor_host:
            address = ipaddress.ip_address(self.executor_host)
            if address.is_unspecified or not (address.is_private or address.is_loopback):
                raise ValueError("Bind the executor to a specific local/private interface")
            if not all((self.executor_origin, self.tls_certificate, self.tls_private_key, self.tls_client_ca)):
                raise ValueError("The executor requires its HTTPS origin, server certificate, key and client CA")
            if not self.executor_origin.startswith("https://"):
                raise ValueError("Executor origin must use HTTPS")
        return self

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @classmethod
    def load(cls, path: Path | None = None, **overrides: object) -> Settings:
        values = json.loads(path.read_text()) if path else {}
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(values)
