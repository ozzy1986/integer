"""How a fake provider behaves: global settings, per-task prompt directives, and the random plan.

Directives let a test or the demo UI script one task without touching global settings, e.g.
"a cat @alpha:fail=moderation @beta:dup=10". The service never parses them; it just forwards the prompt.

    @<provider>:fail[=technical|moderation|payload]   fail in the webhook
    @<provider>:sync=503|timeout                       fail the POST /tasks itself
    @<provider>:dup=N / dupseq=N                       deliver the webhook N times, concurrently / one by one
    @<provider>:drop                                   never deliver the webhook
    @<provider>:late[=S]                               deliver `completed` after the service stopped waiting
    @<provider>:delay=S                                deliver after exactly S seconds
    @<provider>:ok                                     succeed, whatever the global settings say
"""

import random
import re
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ErrorType = Literal["technical", "moderation", "payload"]
Rate = float  # documented by Field(ge=0, le=1) below

_DIRECTIVE = re.compile(r"@(?P<provider>[a-z0-9-]+):(?P<key>[a-z]+)(?:=(?P<value>[\w.-]+))?")


class Behaviour(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delay_min: float = Field(2.0, ge=0, le=600)
    delay_max: float = Field(20.0, ge=0, le=600)
    fail_rate: Rate = Field(0.0, ge=0, le=1)
    fail_type: ErrorType = "technical"
    sync_fail_rate: Rate = Field(0.0, ge=0, le=1)
    sync_fail: Literal["503", "timeout"] = "503"
    sync_timeout_seconds: float = Field(30.0, ge=0, le=600, description="how long a 'timeout' hangs")
    duplicates: int = Field(1, ge=1, le=20)
    duplicates_concurrent: bool = True
    drop_rate: Rate = Field(0.0, ge=0, le=1)
    late_rate: Rate = Field(0.0, ge=0, le=1)
    late_delay: float = Field(75.0, ge=0, le=3600, description="longer than the service's PROVIDER_TIMEOUT")

    @model_validator(mode="after")
    def _delays_are_ordered(self) -> Self:
        if self.delay_max < self.delay_min:
            raise ValueError("delay_max must be >= delay_min")
        return self


def _directive_update(key: str, value: str | None) -> dict[str, Any] | None:
    match key:
        case "fail":
            return {"fail_rate": 1.0, "fail_type": value or "technical"}
        case "sync":
            return {"sync_fail_rate": 1.0, "sync_fail": value} if value else None
        case "dup" | "dupseq":
            return {"duplicates": value, "duplicates_concurrent": key == "dup"} if value else None
        case "drop":
            return {"drop_rate": 1.0}
        case "late":
            return {"late_rate": 1.0, "late_delay": value} if value else {"late_rate": 1.0}
        case "delay":
            return {"delay_min": value, "delay_max": value} if value else None
        case "ok":
            return {"fail_rate": 0.0, "sync_fail_rate": 0.0, "drop_rate": 0.0, "late_rate": 0.0}
    return None


def apply_directives(base: Behaviour, prompt: str, provider: str) -> Behaviour:
    """Overlay this provider's directives from the prompt; invalid ones are ignored one by one."""
    behaviour = base
    for match in _DIRECTIVE.finditer(prompt):
        if match["provider"] != provider:
            continue
        update = _directive_update(match["key"], match["value"])
        if update is None:
            continue
        try:
            behaviour = Behaviour.model_validate({**behaviour.model_dump(), **update})
        except ValidationError:
            continue
    return behaviour


@dataclass(frozen=True, slots=True)
class Plan:
    sync_fail: Literal["503", "timeout"] | None
    outcome: Literal["completed", "failed"]
    error_type: ErrorType | None
    deliver: bool
    copies: int
    concurrent: bool
    delay: float


def make_plan(behaviour: Behaviour, rng: random.Random) -> Plan:
    sync_fail = behaviour.sync_fail if rng.random() < behaviour.sync_fail_rate else None
    failed = rng.random() < behaviour.fail_rate
    late = not failed and rng.random() < behaviour.late_rate
    deliver = rng.random() >= behaviour.drop_rate
    delay = behaviour.late_delay if late else rng.uniform(behaviour.delay_min, behaviour.delay_max)
    return Plan(
        sync_fail=sync_fail,
        outcome="failed" if failed else "completed",
        error_type=behaviour.fail_type if failed else None,
        deliver=deliver,
        copies=behaviour.duplicates,
        concurrent=behaviour.duplicates_concurrent,
        delay=delay,
    )
