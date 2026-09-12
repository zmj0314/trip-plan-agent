"""What a *live* model actually emits, pinned as a regression.

Every case here was observed against ``deepseek-chat`` and every one of them kept
a real request stuck at the information gate. The offline stub had never produced
any of these shapes, which is exactly why they survived 279 passing tests: a stub
only exercises the vocabulary its author thought of.

If these assertions ever fail after a prompt change, the change has re-broken
"the gate releases on a real request".
"""

from __future__ import annotations

import pytest

from app.domain.models import SlotValue
from app.policy import segments as segment_policy
from app.policy import slots as slot_policy


def _slot(value):
    return SlotValue.filled(value)


# ---------------------------------------------------------------------------
# Field names the model uses that the registry did not know
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("emitted", "expected"),
    [
        ("return_by", "return_date"),
        ("return", "return_date"),
        ("end_date", "return_date"),
        ("duration_days", "trip_days_hint"),
        ("days", "trip_days_hint"),
        ("nights", "trip_days_hint"),
        ("accommodation", "lodging"),
        ("hotel", "lodging"),
        ("destinations", "destinations"),
    ],
)
def test_live_field_names_map_onto_the_registry(emitted: str, expected: str) -> None:
    assert slot_policy.canonical_slot_id(emitted) == expected


def test_destination_does_not_swallow_destinations() -> None:
    """Prefix collision: getting this wrong deletes every city after the first."""

    assert slot_policy.canonical_slot_id("destinations") != slot_policy.canonical_slot_id("destination")


def test_destination_is_kept_as_a_gate_slot() -> None:
    """The multi-city list must not be merged into ``destination``.

    The gate checks ``destination``; the day planner needs the ordered list. They
    are two slots for two purposes, and merging them silently loses the split.
    """

    assert slot_policy.SLOTS_BY_ID["destination"].requirement is slot_policy.Requirement.ALWAYS
    assert slot_policy.SLOTS_BY_ID["destinations"].requirement is slot_policy.Requirement.DERIVED


# ---------------------------------------------------------------------------
# Values the model writes where the registry expects a vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("emitted", "expected"),
    [
        ("self_arranged", "none"),
        ("不需要住宿", "none"),
        ("自己订", "none"),
        ("none", "none"),
        ("需要安排住宿", "need"),
        ("需要", "need"),
    ],
)
def test_lodging_answers_are_normalised(emitted: str, expected: str) -> None:
    assert slot_policy.normalise_lodging(emitted) == expected


def test_an_unrecognised_lodging_answer_is_not_invented() -> None:
    """Unclear input stays unclear; the caller can still show the raw text."""

    assert slot_policy.normalise_lodging("看情况") is None


@pytest.mark.parametrize(
    ("emitted", "expected"),
    [
        ("2026-09-12 当天返回", "2026-09-12"),
        ("2026-10-05", "2026-10-05"),
        ("2026/10/05", "2026-10-05"),
        ("2026.10.05 回来", "2026-10-05"),
    ],
)
def test_a_date_with_trailing_prose_still_yields_a_date(emitted: str, expected: str) -> None:
    assert slot_policy.normalise_date(emitted) == expected


@pytest.mark.parametrize("emitted", ["当天返回", "当天往返", "当日来回"])
def test_same_day_phrases_are_not_dates(emitted: str) -> None:
    """``当天返回`` is an answer, but not to ``return_date``."""

    assert slot_policy.normalise_date(emitted) is None


# ---------------------------------------------------------------------------
# The destination list the model hands over
# ---------------------------------------------------------------------------


def test_a_destinations_list_is_rendered_with_the_users_day_split() -> None:
    """Flattening the list to "北京、天津" is what let a 3/2 request become 3/1."""

    rendered = slot_policy.normalise_slot_value(
        "destinations", [{"city": "北京", "days": 3}, {"city": "天津", "days": 2}]
    )
    assert rendered == "北京3天，天津2天"

    parsed = segment_policy.parse_segments(rendered)
    assert [(spec.city, spec.days) for spec in parsed.segments] == [("北京", 3), ("天津", 2)]


def test_a_destinations_list_without_days_still_parses() -> None:
    rendered = slot_policy.normalise_slot_value(
        "destinations", [{"city": "北京", "days": None}, "天津"]
    )
    assert rendered == "北京，天津"


# ---------------------------------------------------------------------------
# A duration the user stated, when the dates cannot supply one
# ---------------------------------------------------------------------------


def test_a_stated_duration_fills_the_trip_length() -> None:
    slots = {"trip_days_hint": _slot(5)}
    days, notes = slot_policy.derive_trip_days_from_hint(slots)
    assert days == 5
    assert notes == []


def test_an_overlong_stated_duration_is_trimmed_and_disclosed() -> None:
    slots = {"trip_days_hint": _slot(40)}
    days, notes = slot_policy.derive_trip_days_from_hint(slots)
    assert days == slot_policy.MAX_TRIP_DAYS
    assert any("上限" in note for note in notes)


def test_an_unreadable_duration_is_ignored() -> None:
    for value in ("几天", "", None, 0):
        days, _ = slot_policy.derive_trip_days_from_hint({"trip_days_hint": _slot(value)})
        assert days is None, f"{value!r} must not become a trip length"


def test_the_hint_is_used_only_when_the_dates_cannot_answer() -> None:
    """Dates win; the stated duration is a fallback, not a rival."""

    slots = {
        "depart_date": _slot("2026-10-01"),
        "return_date": _slot("2026-10-05"),
        "trip_days_hint": _slot(9),
    }
    result = slot_policy.validate(slots, context={}, rounds_used=0, max_rounds=4)
    assert result.derived["trip_days"] == 5, "the dates describe the trip the user booked"


def test_the_hint_rescues_a_trip_whose_dates_are_missing() -> None:
    slots = {"trip_days_hint": _slot(5)}
    result = slot_policy.validate(slots, context={}, rounds_used=4, max_rounds=4)
    assert result.derived["trip_days"] == 5
