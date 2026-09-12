"""The information gate can only work if everyone agrees what a slot is called.

``app/policy/slots.py`` owns the registry vocabulary; the prompt, the offline
extractor and the planning node all *emit* slot names. When those two dialects
drift, every slot looks empty and the gate either asks forever or silently
substitutes defaults for values the user actually gave -- so both directions of
that seam are pinned here.
"""

from __future__ import annotations

import pytest

from app.domain.models import SlotStatus, SlotValue
from app.policy import slots as slot_policy

REQUIRED = [
    spec for spec in slot_policy.SLOT_REGISTRY if spec.requirement is slot_policy.Requirement.ALWAYS
]

DERIVED = [
    spec for spec in slot_policy.SLOT_REGISTRY if spec.requirement is slot_policy.Requirement.DERIVED
]


def _all_required_filled(except_slot: str | None = None) -> dict[str, SlotValue]:
    slots = {
        spec.slot_id: SlotValue.filled(spec.default or "值")
        for spec in REQUIRED
        if spec.slot_id != except_slot
    }
    # The two dates must be real dates or the DERIVED length cannot be computed
    # and every gate assertion would fail for the wrong reason.
    if except_slot != "depart_date":
        slots["depart_date"] = SlotValue.filled("2026-10-01")
    if except_slot != "return_date":
        slots["return_date"] = SlotValue.filled("2026-10-02")
    return slots


def test_the_registry_actually_has_required_slots():
    assert REQUIRED, "a gate with nothing to block on cannot satisfy §12 #1"


@pytest.mark.parametrize("spec", REQUIRED, ids=lambda spec: spec.slot_id)
def test_every_required_slot_blocks_the_gate_on_its_own(spec):
    """§12 #1: empty any single required slot -> still clarification, never READY."""

    result = slot_policy.validate(
        _all_required_filled(except_slot=spec.slot_id),
        context={},
        rounds_used=0,
        max_rounds=4,
    )
    assert result.ok is False
    assert spec.slot_id in result.missing


def test_the_gate_opens_once_every_required_slot_is_resolved():
    result = slot_policy.validate(_all_required_filled(), context={}, rounds_used=0, max_rounds=4)
    assert result.ok is True
    assert result.missing == []


def test_every_extraction_alias_points_at_a_registered_slot():
    for alias, canonical in slot_policy.SLOT_ALIASES.items():
        assert canonical in slot_policy.SLOTS_BY_ID, f"{alias} -> unknown slot {canonical}"


def test_alias_lookup_is_identity_for_registry_names_and_free_fields():
    for spec in slot_policy.SLOT_REGISTRY:
        assert slot_policy.canonical_slot_id(spec.slot_id) == spec.slot_id
    # Unknown keys are legitimate extra context, not garbage to be dropped.
    assert slot_policy.canonical_slot_id("preference_walk") == "preference_walk"


def test_specific_aliases_win_over_their_own_prefixes():
    """``destination`` is a prefix of ``destinations``; the longer one must win.

    Getting this wrong silently collapses a multi-city answer into one city.
    """

    assert slot_policy.canonical_slot_id("destinations") == "destinations"
    assert slot_policy.canonical_slot_id("destination") == "destination"
    assert slot_policy.canonical_slot_id("return_by") == "return_date"
    assert slot_policy.canonical_slot_id("return") == "return_date"
    assert slot_policy.canonical_slot_id("duration_days") == "trip_days_hint"


def test_optional_slots_never_block_but_are_always_disclosed():
    """DE-3: a decision made on the user's behalf has to be visible."""

    slots = _all_required_filled()
    result = slot_policy.validate(slots, context={}, rounds_used=0, max_rounds=4)
    assert result.ok is True

    notes = slot_policy.unstated_optional(slots, context={})
    assert notes, "silently defaulting every optional slot is the failure mode this guards"
    assert len(notes) == len(
        [s for s in slot_policy.SLOT_REGISTRY if s.requirement is slot_policy.Requirement.OPTIONAL]
    )


def test_optional_assumptions_disappear_once_the_user_states_them():
    slots = _all_required_filled()
    slots["budget"] = SlotValue.filled("2000 以内")
    notes = slot_policy.unstated_optional(slots, context={})

    budget_note = slot_policy.assumption_text(slot_policy.SLOTS_BY_ID["budget"])
    assert budget_note not in notes, "a stated optional slot must not still be disclosed as assumed"
    # The lodging budget is a different optional slot and stays disclosed.
    assert slot_policy.assumption_text(slot_policy.SLOTS_BY_ID["lodging_budget"]) in notes


def test_delegation_does_not_get_stuck_on_unreadable_dates():
    """DC-2: a delegating user must never loop on a slot they declined."""

    from app.domain.models import SlotStatus as _Status

    slots = {"depart_date": SlotValue.declined("用户未指定，取最近的周末")}
    slots["return_date"] = SlotValue.declined("用户未指定，按当日往返处理")
    days, issues, notes = slot_policy.derive_trip_days(slots)

    assert days is None
    assert issues == []
    assert _Status.FILLED.resolved and _Status.USER_DECLINED.resolved


def test_delegation_resolves_the_remaining_required_slots():
    """DC-2: "你定" is an answer, not a dodge."""

    slots: dict[str, SlotValue] = {}
    assert slot_policy.is_delegation("其余的你都帮我定吧")
    assumptions = slot_policy.decline_remaining(slots, context={})

    assert assumptions, "the user must be told which defaults were adopted"
    assert set(slots) == {spec.slot_id for spec in REQUIRED}
    assert all(value.status is SlotStatus.USER_DECLINED for value in slots.values())

    result = slot_policy.validate(slots, context={}, rounds_used=0, max_rounds=4)
    assert result.ok is True, "a delegating user must be able to leave clarification"


def test_intent_only_activates_the_identity_slot_for_bookings():
    advice = {"intent": "advice_only"}
    assert "rider_identity" not in {spec.slot_id for spec in slot_policy.active_required(advice)}

    booking = {"intent": "booking"}
    assert "rider_identity" in {spec.slot_id for spec in slot_policy.active_required(booking)}


# ---------------------------------------------------------------------------
# DERIVED slots (F1): trip length comes from the two dates, never from a question.
# ---------------------------------------------------------------------------


def test_derived_slots_are_never_gate_conditions():
    assert DERIVED, "F1 relies on trip_days being derivable"
    assert all(spec.slot_id not in {s.slot_id for s in REQUIRED} for spec in DERIVED)
    assert not any(spec in slot_policy.active_required({}) for spec in DERIVED)


def test_trip_days_are_derived_from_the_two_dates_without_asking():
    slots = _all_required_filled()
    slots["depart_date"] = SlotValue.filled("2026-10-01")
    slots["return_date"] = SlotValue.filled("2026-10-05")
    result = slot_policy.validate(slots, context={}, rounds_used=0, max_rounds=4)

    assert result.ok is True
    assert result.derived["trip_days"] == 5
    assert "trip_days" not in result.missing


def test_return_before_departure_is_a_contradiction_not_a_guess():
    slots = _all_required_filled()
    slots["depart_date"] = SlotValue.filled("2026-10-05")
    slots["return_date"] = SlotValue.filled("2026-10-01")
    result = slot_policy.validate(slots, context={}, rounds_used=0, max_rounds=4)

    assert result.ok is False
    assert result.issues
    assert "return_date" in result.missing


def test_overlong_trip_is_trimmed_and_disclosed():
    slots = _all_required_filled()
    slots["depart_date"] = SlotValue.filled("2026-10-01")
    slots["return_date"] = SlotValue.filled("2026-11-15")
    result = slot_policy.validate(slots, context={}, rounds_used=0, max_rounds=4)

    assert result.ok is True
    assert result.derived["trip_days"] == slot_policy.MAX_TRIP_DAYS
    assert any("上限" in note for note in result.assumptions)


def test_missing_return_date_falls_back_to_one_day_with_an_assumption():
    slots = _all_required_filled(except_slot="return_date")
    result = slot_policy.validate(slots, context={}, rounds_used=4, max_rounds=4)

    assert result.ok is True
    assert result.derived["trip_days"] == 1
    assert any("当日往返" in note for note in result.assumptions)


def test_intent_slot_values_are_translated_into_the_condition_vocabulary():
    """The schema's three intents collapse to the spec's two branches."""

    cases = (("advice_only", "advice_only"), ("booking_list", "booking"), ("reservation", "booking"))
    for raw, expected in cases:
        context = slot_policy.context_from_slots({"intent": SlotValue.filled(raw)})
        assert context["intent"] == expected
