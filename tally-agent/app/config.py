"""Runtime configuration. All settings come from the environment / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- database -------------------------------------------------------
    pg_dsn: str = "postgresql://tally_ro:devro@127.0.0.1:5432/tally"
    pg_pool_min: int = 1
    pg_pool_max: int = 8
    statement_timeout_ms: int = 15_000

    # --- redis ----------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"
    token_ttl_seconds: int = 3600
    cache_ttl_seconds: int = 120

    # --- local LLM ------------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:14b"
    llm_timeout_seconds: int = 45
    llm_enabled: bool = True

    # --- live Tally fallback (read-only). Empty disables it. ------------
    tally_live_url: str = ""
    tally_live_timeout: int = 10

    # --- service --------------------------------------------------------
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    max_rows: int = 20_000      # rows a report may return; totals cover all of them
    sync_stale_warn_minutes: int = 10

    # Salt for audit-log question hashes. MUST be set in production.
    audit_hash_salt: str = "change-me-in-production"

    # --- access ---------------------------------------------------------
    users_file: str = "config/users.json"
    rate_limit_per_minute: int = 30
    # If the audit row cannot be written, refuse to return data (DPDP posture).
    audit_fail_closed: bool = True

    # --- answering ------------------------------------------------------
    llm_narrate: bool = True          # add a model-written, grounding-checked explanation
    # Pin "today" for a test company whose books end in the past (YYYY-MM-DD).
    reference_date: str = ""


settings = Settings()
