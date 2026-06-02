"""Environment-driven configuration for Caldera.

All settings are read from environment variables prefixed ``CALDERA_`` (or a
local ``.env`` file). See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CALDERA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API / server ────────────────────────────────────────────────
    # NoDecode: let our validator split the comma-separated env string instead
    # of pydantic-settings trying to JSON-decode it first.
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    read_only: bool = False
    max_note_bytes: int = 5 * 1024 * 1024
    search_fuzzy_threshold: float = 60.0  # min keyword-search score 0–100 (SEARCH.md §7)
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # ── Vault source ────────────────────────────────────────────────
    source: Literal["github", "local"] = "github"
    vault_path: str = "/vault"

    github_repo: str | None = None
    github_token: str | None = None
    github_branch: str = "main"

    # ── Sync loop ───────────────────────────────────────────────────
    sync_interval: int = 60
    commit_debounce: float = 10.0  # seconds of write-quiet before a flush (§5.3)
    commit_max_wait: float = 30.0  # forced-flush ceiling from first pending write
    push_wedged_after: int = 5  # consecutive push failures → push_wedged (§7.2.1)
    git_author_name: str = "Caldera"
    git_author_email: str = "caldera@localhost"

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, v: object) -> object:
        """Accept a comma-separated string from the environment."""
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    @property
    def github_remote_url(self) -> str:
        """Authenticated HTTPS clone URL for the configured GitHub repo."""
        if not self.github_repo or not self.github_token:
            raise ValueError("github_repo and github_token must be set for the github source")
        return f"https://x-access-token:{self.github_token}@github.com/{self.github_repo}.git"


@lru_cache
def get_settings() -> Settings:
    return Settings()
