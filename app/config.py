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

    max_active_tasks: int = Field(3, ge=1)
    max_body_bytes: int = Field(64 * 1024, ge=1024)
    ledger_page_size: int = Field(20, ge=1, le=200)

    # When set, every user-facing request must carry valid Telegram Mini App initData for that user.
    telegram_bot_token: SecretStr | None = None
    telegram_init_data_ttl: int = Field(24 * 3600, gt=0)

    # Demo UI top-ups go through the fake payment provider so they arrive as real signed webhooks.
    demo_payments_url: str | None = None
    demo_max_topup: int = Field(1000, ge=1)

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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
