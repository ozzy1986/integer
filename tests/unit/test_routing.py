"""Leg selection rules 1-2 from the task statement, as a pure function."""

import pytest

from app.domain import Leg, ProviderError, first_leg, next_leg

IMAGE_FAST = (Leg(1, "alpha", 4, False), Leg(2, "beta", 10, False))
VIDEO_LONG = (Leg(1, "beta", 2, False), Leg(2, "alpha", 1, True))


def providers(*legs: Leg | None) -> list[str | None]:
    return [leg.provider if leg else None for leg in legs]


@pytest.mark.parametrize(
    ("legs", "refs", "expected"),
    [
        (IMAGE_FAST, 0, "alpha"),
        (IMAGE_FAST, 4, "alpha"),
        (IMAGE_FAST, 5, "beta"),  # alpha takes at most 4 references
        (IMAGE_FAST, 10, "beta"),
        (IMAGE_FAST, 11, None),  # nobody takes 11 -> 400 before charging
        (VIDEO_LONG, 2, "beta"),
        (VIDEO_LONG, 3, None),
    ],
)
def test_first_leg_must_accept_all_references(legs: tuple[Leg, ...], refs: int, expected: str | None) -> None:
    assert providers(first_leg(legs, refs)) == [expected]


def test_legs_are_tried_in_priority_order_regardless_of_input_order() -> None:
    assert providers(first_leg(tuple(reversed(IMAGE_FAST)), 0)) == ["alpha"]


def test_technical_failure_moves_to_next_leg() -> None:
    assert providers(next_leg(IMAGE_FAST, 0, 1, ProviderError.TECHNICAL)) == ["beta"]


def test_moderation_failure_moves_to_next_leg_without_skip_flag() -> None:
    assert providers(next_leg(IMAGE_FAST, 0, 1, ProviderError.MODERATION)) == ["beta"]


def test_moderation_failure_skips_legs_with_skip_on_moderation() -> None:
    assert next_leg(VIDEO_LONG, 0, 1, ProviderError.MODERATION) is None


def test_skip_on_moderation_does_not_apply_to_technical_failures() -> None:
    assert providers(next_leg(VIDEO_LONG, 1, 1, ProviderError.TECHNICAL)) == ["alpha"]


def test_payload_failure_stops_the_chain() -> None:
    assert next_leg(IMAGE_FAST, 0, 1, ProviderError.PAYLOAD) is None


def test_next_leg_also_has_to_accept_the_references() -> None:
    # video-long with 2 refs: beta fits, alpha takes only 1 -> no fallback.
    assert next_leg(VIDEO_LONG, 2, 1, ProviderError.TECHNICAL) is None


def test_no_legs_after_the_last_one() -> None:
    assert next_leg(IMAGE_FAST, 0, 2, ProviderError.TECHNICAL) is None


def test_skipped_leg_does_not_reset_the_moderation_rule() -> None:
    legs = (Leg(1, "a", 5, False), Leg(2, "b", 5, True), Leg(3, "c", 5, True), Leg(4, "d", 5, False))
    assert providers(next_leg(legs, 0, 1, ProviderError.MODERATION)) == ["d"]
