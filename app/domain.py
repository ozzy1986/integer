"""Pure domain rules: statuses and leg selection. No I/O here, so the rules are trivially testable."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class TaskStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AttemptStatus(StrEnum):
    PENDING = "pending"  # created, waiting for the dispatcher to send it
    SENT = "sent"  # provider accepted it (202), waiting for the webhook or the watchdog
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"  # an earlier attempt delivered a late result; this one is no longer needed


LIVE_ATTEMPT_STATUSES = (AttemptStatus.PENDING, AttemptStatus.SENT)


class ProviderError(StrEnum):
    TECHNICAL = "technical"
    MODERATION = "moderation"
    PAYLOAD = "payload"


@dataclass(frozen=True, slots=True)
class Leg:
    priority: int
    provider: str
    max_ref_images: int
    skip_on_moderation: bool

    def accepts(self, ref_count: int) -> bool:
        return ref_count <= self.max_ref_images


def next_leg(legs: Iterable[Leg], ref_count: int, after_priority: int, last_error: ProviderError | None) -> Leg | None:
    """The leg to try after `after_priority` failed with `last_error` (rules 1-2)."""
    if last_error is ProviderError.PAYLOAD:
        return None  # a malformed request is rejected by any leg
    for leg in sorted(legs, key=lambda candidate: candidate.priority):
        if leg.priority <= after_priority or not leg.accepts(ref_count):
            continue
        if last_error is ProviderError.MODERATION and leg.skip_on_moderation:
            continue  # the last real attempt failed moderation; skipped legs do not reset that
        return leg
    return None


def first_leg(legs: Iterable[Leg], ref_count: int) -> Leg | None:
    return next_leg(legs, ref_count, after_priority=0, last_error=None)
