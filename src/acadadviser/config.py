from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    site_domain: str = ""
    contact_email: str = ""
    cookie_secure: bool = True
    psa_url: str = "http://127.0.0.1:9003"
    sip_url: str = "http://127.0.0.1:9001"
    data_dir: Path = _REPO_ROOT / "data" / "gateway"
    searches_per_ip_per_day: int = 5
    model_runs_per_day: int = 50
    follow_ups_per_search: int = 8
    concurrent_uploads: int = 3
    max_file_bytes: int = 10 * 1024 * 1024
    max_text_chars: int = 4000


@lru_cache
def get_settings() -> Settings:
    return Settings()
