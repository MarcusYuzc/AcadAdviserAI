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
    # Postgres when DB_HOST is set (ECS gets these from Secrets Manager); otherwise SQLite in data_dir.
    database_url: str = ""
    db_host: str = ""
    db_port: int = 5432
    db_name: str = "acadadviser"
    db_user: str = "gateway_app"
    db_password: str = ""
    # Peers allowed to set X-Forwarded-For: loopback for Caddy, the VPC for the AWS load balancer.
    trusted_proxy_cidrs: str = "127.0.0.1/32,::1/128"
    searches_per_ip_per_day: int = 5
    model_runs_per_day: int = 50
    follow_ups_per_search: int = 8
    concurrent_uploads: int = 3
    # Accounts: the browser signs in with Cognito and the gateway verifies the ID token.
    cognito_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    # Billing: one yearly Stripe price. A free search comes first, with fewer refinements.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""
    plan_price_label: str = "$99 per year"
    free_follow_ups: int = 3
    searches_per_user_per_day: int = 15
    max_file_bytes: int = 10 * 1024 * 1024
    max_text_chars: int = 4000

    def database_target(self) -> Path | str:
        if self.database_url:
            return self.database_url
        if self.db_host:
            from psycopg.conninfo import make_conninfo

            return make_conninfo(
                host=self.db_host, port=self.db_port, dbname=self.db_name, user=self.db_user,
                password=self.db_password, sslmode="require",
            )
        return self.data_dir / "gateway.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
