"""Centralised application settings loaded from environment / .env file.

All variables share the ``VCP_CP_`` prefix so they don't clash with the
data-plane worker variables (``VCP_DP_``) when both run on the same host.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VCP_CP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # API server
    # -------------------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8090
    api_token: str = ""
    require_auth: bool = True

    # -------------------------------------------------------------------------
    # PostgreSQL (async via asyncpg)
    # -------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://vcp:vcp@localhost:5432/vcp_control"

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str | None = None
    redis_ssl: bool = False
    redis_db: int = 0

    # -------------------------------------------------------------------------
    # Command/Event bus — key prefixes must match vcp_data_plane settings
    # -------------------------------------------------------------------------
    command_stream_prefix: str = "vcp:commands:worker"
    broadcast_stream: str = "vcp:commands:broadcast"
    event_stream_prefix: str = "vcp:events:worker"
    consumer_group: str = "control-plane"
    consumer_name: str = "cp-01"
    # How long to block on XREADGROUP (ms); 0 = forever
    stream_block_ms: int = 1_000
    # How many events to pull per read
    stream_batch_size: int = 50

    # -------------------------------------------------------------------------
    # Data-plane worker HTTP endpoints (for direct status queries)
    # -------------------------------------------------------------------------
    # Comma-separated list: "http://worker-01:8080,http://worker-02:8080"
    worker_urls_raw: str = Field(default="", alias="VCP_CP_WORKER_URLS")
    worker_token: str = ""

    @field_validator("worker_urls_raw", mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> str:
        return str(v) if v is not None else ""

    @property
    def worker_urls(self) -> list[str]:
        return [u.strip() for u in self.worker_urls_raw.split(",") if u.strip()]

    # -------------------------------------------------------------------------
    # Frontend / CORS
    # -------------------------------------------------------------------------
    frontend_url: str = "http://localhost:5173"

    # -------------------------------------------------------------------------
    # Profile directory (for YAML profile import)
    # -------------------------------------------------------------------------
    profile_dir: str = "configs/profiles"

    # -------------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------------
    log_level: str = "INFO"
    # Every message read from the Redis event streams is also appended here
    # (rotating, 10 MB x 5). Set to "" to disable the file and log to console only.
    redis_event_log_file: str = "logs/redis_events.log"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
