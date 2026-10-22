"""Pure parts of the fake providers: prompt directives and the per-task plan."""

import random

import pytest

from fake_providers.behaviour import Behaviour, Plan, apply_directives, make_plan

BASE = Behaviour(delay_min=2, delay_max=20)


def test_directives_for_other_providers_are_ignored() -> None:
    assert apply_directives(BASE, "a cat @beta:fail=moderation", "alpha") == BASE


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("@alpha:fail", {"fail_rate": 1.0, "fail_type": "technical"}),
        ("@alpha:fail=moderation", {"fail_rate": 1.0, "fail_type": "moderation"}),
        ("@alpha:fail=payload", {"fail_rate": 1.0, "fail_type": "payload"}),
        ("@alpha:sync=503", {"sync_fail_rate": 1.0, "sync_fail": "503"}),
        ("@alpha:sync=timeout", {"sync_fail_rate": 1.0, "sync_fail": "timeout"}),
        ("@alpha:dup=10", {"duplicates": 10, "duplicates_concurrent": True}),
        ("@alpha:dupseq=3", {"duplicates": 3, "duplicates_concurrent": False}),
        ("@alpha:drop", {"drop_rate": 1.0}),
        ("@alpha:late", {"late_rate": 1.0}),
        ("@alpha:late=130", {"late_rate": 1.0, "late_delay": 130.0}),
        ("@alpha:delay=3.5", {"delay_min": 3.5, "delay_max": 3.5}),
        ("@alpha:ok", {"fail_rate": 0.0, "sync_fail_rate": 0.0, "drop_rate": 0.0, "late_rate": 0.0}),
    ],
)
def test_directive_overrides(prompt: str, expected: dict[str, object]) -> None:
    assert apply_directives(BASE, f"a cat in a hat {prompt}", "alpha") == BASE.model_copy(update=expected)


def test_several_directives_combine() -> None:
    behaviour = apply_directives(BASE, "@alpha:fail=moderation @alpha:dup=2 @beta:drop", "alpha")
    assert (behaviour.fail_type, behaviour.duplicates, behaviour.drop_rate) == ("moderation", 2, 0.0)


@pytest.mark.parametrize(
    "prompt", ["@alpha:fail=cosmic", "@alpha:dup=0", "@alpha:dup=999", "@alpha:delay=-1", "@alpha:what"]
)
def test_invalid_directives_are_ignored(prompt: str) -> None:
    assert apply_directives(BASE, prompt, "alpha") == BASE


def test_plan_for_a_healthy_provider_completes_after_a_delay_in_range() -> None:
    plan = make_plan(BASE, random.Random(1))
    assert (plan.sync_fail, plan.outcome, plan.deliver, plan.copies) == (None, "completed", True, 1)
    assert 2 <= plan.delay <= 20


def test_plan_honours_certain_failures() -> None:
    behaviour = BASE.model_copy(update={"fail_rate": 1.0, "fail_type": "moderation"})
    assert make_plan(behaviour, random.Random(1)) == Plan(
        sync_fail=None, outcome="failed", error_type="moderation", deliver=True, copies=1, concurrent=True,
        delay=make_plan(behaviour, random.Random(1)).delay,
    )  # fmt: skip


def test_late_completion_waits_longer_than_the_service_timeout() -> None:
    behaviour = BASE.model_copy(update={"late_rate": 1.0, "late_delay": 75})
    plan = make_plan(behaviour, random.Random(1))
    assert (plan.outcome, plan.delay) == ("completed", 75)


def test_fail_rate_is_a_probability() -> None:
    behaviour = BASE.model_copy(update={"fail_rate": 0.3})
    rng = random.Random(42)
    failures = sum(make_plan(behaviour, rng).outcome == "failed" for _ in range(2000))
    assert 500 < failures < 700
