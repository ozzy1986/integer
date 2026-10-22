"""Request and response models. Requests are strict: unknown fields are rejected, sizes are bounded."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, UrlConstraints, model_validator

from app.billing import PaymentStatus
from app.domain import AttemptStatus, ProviderError, TaskStatus

UserId = Annotated[int, Field(gt=0, lt=2**63)]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
HttpUrl = Annotated[AnyHttpUrl, UrlConstraints(max_length=2048, allowed_schemes=["http", "https"])]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerateIn(StrictModel):
    user_id: UserId
    model_slug: Slug
    prompt: str = Field(min_length=1, max_length=4000)
    ref_images: list[HttpUrl] = Field(default_factory=list, max_length=32)


class GenerateOut(BaseModel):
    task_id: UUID
    status: TaskStatus


class ProviderWebhookIn(BaseModel):
    # Not strict: providers may add fields over time; we read only what we need.
    attempt_id: UUID
    external_id: str | None = Field(None, max_length=128)
    status: Literal["completed", "failed"]
    result_url: HttpUrl | None = None
    error_type: ProviderError | None = None

    @model_validator(mode="after")
    def _outcome_has_its_data(self) -> Self:
        if self.status == "completed" and self.result_url is None:
            raise ValueError("completed webhook needs result_url")
        if self.status == "failed" and self.error_type is None:
            raise ValueError("failed webhook needs error_type")
        return self


class PaymentWebhookIn(BaseModel):
    payment_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    user_id: UserId
    tokens: int = Field(gt=0, le=1_000_000_000)
    status: PaymentStatus


class WebhookOut(BaseModel):
    outcome: str


class PaymentOut(BaseModel):
    credited: bool


class LedgerRow(BaseModel):
    id: int
    kind: Literal["payment", "charge", "refund"]
    amount: int
    balance_after: int
    task_id: UUID | None
    payment_id: str | None
    created_at: datetime


class BalanceOut(BaseModel):
    user_id: int
    balance: int
    ledger: list[LedgerRow]


class AttemptOut(BaseModel):
    id: UUID
    number: int
    provider: str
    leg_priority: int
    status: AttemptStatus
    error: ProviderError | None
    reason: str | None
    external_id: str | None
    result_url: str | None
    created_at: datetime
    sent_at: datetime | None
    deadline_at: datetime | None = Field(description="when the watchdog gives up on this attempt")
    finished_at: datetime | None


class CurrentLeg(BaseModel):
    attempt: int
    priority: int
    provider: str
    status: AttemptStatus


class EventOut(BaseModel):
    kind: str
    attempt_id: UUID | None
    detail: dict[str, Any]
    created_at: datetime


class TaskSummary(BaseModel):
    id: UUID
    user_id: int
    model: str
    status: TaskStatus
    price: int
    result_url: str | None
    error: ProviderError | None
    created_at: datetime
    finished_at: datetime | None


class TaskOut(TaskSummary):
    prompt: str
    ref_images: list[str]
    current_leg: CurrentLeg | None
    attempts: list[AttemptOut]
    events: list[EventOut]


class LegOut(BaseModel):
    priority: int
    provider: str
    max_ref_images: int
    skip_on_moderation: bool


class ModelOut(BaseModel):
    slug: str
    price: int
    legs: list[LegOut]


class HealthOut(BaseModel):
    status: Literal["ok"]


class TopUpIn(StrictModel):
    user_id: UserId
    tokens: int = Field(gt=0)
    copies: int = Field(1, ge=1, le=20, description="identical webhooks the payment provider sends at once")


class TopUpOut(BaseModel):
    payment_id: str
    delivered: list[int]
