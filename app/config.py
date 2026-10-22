"""Service configuration. Everything comes from the environment; secrets never have defaults."""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32

Secret = Annotated[SecretStr, Field(min_length=MIN_SECRET_LENGTH)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    database_url: SecretStr
    db_pool_min: int = Field(1, ge=1)
    db_pool_max: int = Field(10, ge=1)

    # {"alpha": "http://fake-providers:9000/alpha", ...}; each provider signs webhooks with its own secret.
    provider_urls: dict[str, str]
    provider_secrets: dict[str, Secret]
    payments_secret: Secret

    provider_timeout: float = Field(60.0, gt=0, description="Watchdog: how long to wait for a webhook")
    send_timeout: float = Field(10.0, gt=0, description="Timeout of the synchronous POST /tasks")
    dispatch_lease: float = Field(30.0, gt=0, description="A crashed dispatcher's claim expires after this")
    poll_interval: float = Field(0.5, gt=0, description="Background loop period (dispatcher + watchdog)")
    background_jobs: bool = True

    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")
    max_active_tasks: int = Field(3, ge=1)
    max_body_bytes: int = Field(64 * 1024, ge=1024)
    ledger_page_size: int = Field(20, ge=1, le=200)
    # Public endpoints that write (POST /generate, /demo/topup), per client address; 0 turns it off.
    rate_limit_per_minute: int = Field(0, ge=0)
    # GET /health/invariants sums the whole ledger: recompute it at most this often per worker.
    invariants_cache_seconds: float = Field(10.0, ge=0)

    # When set, every user-facing request must carry valid Telegram Mini App initData for that user.
    telegram_bot_token: SecretStr | None = None
    telegram_init_data_ttl: int = Field(24 * 3600, gt=0)

    # Demo UI top-ups go through the fake payment provider so they arrive as real signed webhooks.
    demo_payments_url: str | None = None
    demo_max_topup: int = Field(1000, ge=1)

    @field_validator("telegram_bot_token", "demo_payments_url", mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        # docker compose turns `${VAR:-}` into an empty string; that must mean "off", not "on with ''".
        return None if value == "" else value

    @field_validator("provider_secrets")
    @classmethod
    def _every_provider_has_a_secret(cls, secrets: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if not secrets:
            raise ValueError("at least one provider secret is required")
        return secrets

    def model_post_init(self, __context: object) -> None:
        missing = set(self.provider_urls) - set(self.provider_secrets)
        if missing:
            raise ValueError(f"providers without a webhook secret: {sorted(missing)}")
        if self.dispatch_lease <= self.send_timeout:
            # Otherwise a slow but alive sender could see its attempt re-claimed by another worker.
            raise ValueError("DISPATCH_LEASE must be longer than SEND_TIMEOUT")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
