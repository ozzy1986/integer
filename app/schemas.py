"""Request and response models. Requests are strict: unknown fields are rejected, sizes are bounded."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.billing import PaymentStatus
from app.domain import AttemptStatus, ProviderError, TaskStatus


def _not_bool(value: object) -> object:
    # Lax mode would turn JSON true/false into user 1/0.
    if isinstance(value, bool):
        raise ValueError("must be an integer, not a boolean")
    return value


def _no_nul(value: str) -> str:
    # PostgreSQL text cannot hold NUL: reject it as 422 here instead of failing in the database with a 500.
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


# The task names no payment statuses: common spellings of the same four states are accepted, so a success
# reported as "paid" is credited instead of rejected with 422 (and lost: the sender gives up on a 4xx).
_PAYMENT_STATUS_ALIASES = {
    "success": PaymentStatus.SUCCEEDED,
    "successful": PaymentStatus.SUCCEEDED,
    "paid": PaymentStatus.SUCCEEDED,
    "completed": PaymentStatus.SUCCEEDED,
    "processing": PaymentStatus.PENDING,
    "cancelled": PaymentStatus.CANCELED,
    "declined": PaymentStatus.FAILED,
}


def _payment_status(value: object) -> object:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return _PAYMENT_STATUS_ALIASES.get(normalized, normalized)
    return value


UserId = Annotated[int, BeforeValidator(_not_bool), Field(gt=0, lt=2**63)]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
# A reference image or a provider's result: the task fixes no format (a URL of any scheme, a file name, an
# id), so any short string is passed on as is. Whoever renders one as a link checks its scheme (the UI does).
Reference = Annotated[str, Field(min_length=1, max_length=2048), AfterValidator(_no_nul)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerateIn(StrictModel):
    user_id: UserId
    model_slug: Slug
    prompt: Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_no_nul)]
    # No cap on how many: the body limit bounds the list, and more than any leg takes is rule 1's 400.
    ref_images: list[Reference] = Field(default_factory=list)


class GenerateOut(BaseModel):
    task_id: UUID
    status: TaskStatus


class ProviderWebhookIn(BaseModel):
    # Not strict: providers may add fields over time; we read only what we need.
    attempt_id: UUID
    external_id: str | None = Field(None, max_length=128)
    status: Literal["completed", "failed"]
    # Rejecting a signed s3:// result would make the provider stop retrying and lose a finished job.
    result_url: Reference | None = None
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
    status: Annotated[PaymentStatus, BeforeValidator(_payment_status)]


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
