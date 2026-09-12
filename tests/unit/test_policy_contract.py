"""Contract tests for the deterministic decision layer (framework §2 P1, §6).

The policy package owns the rules the product promises cannot be argued away
by a language model: no call before consent, dynamic transfer buffers, and
weather vetoes that outrank price. Each test below pins one of those promises.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.capabilities.contract import DegradationLevel
from app.capabilities.defaults import build_default_registry
from app.domain.geo import Coord, Crs
from app.errors import CapabilityNotFound, CoordinateSystemMismatch
from app.policy import buffer, idempotency, reason_trace, risk, scoring, weather_rules
from app.policy.constraints import validate_trip

BEIJING = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --- layer boundary ---------------------------------------------------------


def test_policy_layer_pulls_in_no_io_dependencies():
    """§2 hard constraint: policy may not reach for a transport or a database."""

    probe = (
        "import sys; import app.policy; "
        "banned = {'httpx', 'openai', 'sqlite3', 'mcp', 'app.channels', 'app.store', 'app.graph'}; "
        "print(sorted(banned & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "[]"


# --- dynamic buffers (§6.5) -------------------------------------------------


def test_buffer_is_scaled_by_data_uncertainty():
    live = buffer.required_buffer_minutes(
        prev_mode="flight", cross_station=False, uncertainty=DegradationLevel.D0
    )
    cached = buffer.required_buffer_minutes(
        prev_mode="flight", cross_station=False, uncertainty=DegradationLevel.D1
    )
    assert live == 120
    assert cached == 180


def test_cross_station_transfer_dominates_a_short_preceding_mode():
    assert buffer.required_buffer_minutes(
        prev_mode="metro", cross_station=False, uncertainty=DegradationLevel.D0
    ) == 45
    assert buffer.required_buffer_minutes(
        prev_mode="metro", cross_station=True, uncertainty=DegradationLevel.D0
    ) == 90


# --- idempotency (DH-2) -----------------------------------------------------


def test_idem_key_is_stable_under_parameter_permutation():
    first = {
        "trip_date": "2026/9/12",
        "passengers": ["张三", "李四"],
        "spot": {"lat": 40.4319, "lon": 116.5704, "crs": "wgs84"},
        "note": "",
        "ticket_class": ["成人", "成人"],
    }
    second = {
        "ticket_class": ["成人", "成人"],
        "spot": {"crs": "wgs84", "lon": 116.5704, "lat": 40.4319},
        "passengers": ["李四", "张三"],
        "trip_date": "2026-09-12",
        "note": None,
    }
    assert idempotency.idem_key(
        session_id="sess-1", action_kind="booking.reserve", subject=first
    ) == idempotency.idem_key(
        session_id="sess-1", action_kind="booking.reserve", subject=second
    )


def test_idem_key_separates_different_actions():
    subject = {"trip_date": "2026-09-12", "passengers": ["张三"]}
    base = idempotency.idem_key(session_id="sess-1", action_kind="booking.reserve", subject=subject)
    assert base != idempotency.idem_key(
        session_id="sess-1", action_kind="booking.reserve", subject={"trip_date": "2026-09-13", "passengers": ["张三"]}
    )
    assert base != idempotency.idem_key(
        session_id="sess-2", action_kind="booking.reserve", subject=subject
    )
    assert base != idempotency.idem_key(
        session_id="sess-1", action_kind="booking.order", subject=subject
    )


def test_coordinates_are_compared_in_one_crs():
    gcj = Coord(lat=39.9042, lon=116.4074, crs=Crs.GCJ02)
    wgs = gcj.to_wgs84()
    as_gcj = idempotency.idem_key(
        session_id="s",
        action_kind="booking.reserve",
        subject={"spot": {"lat": gcj.lat, "lon": gcj.lon, "crs": "gcj02"}},
    )
    as_wgs = idempotency.idem_key(
        session_id="s",
        action_kind="booking.reserve",
        subject={"spot": {"lat": round(wgs.lat, 4), "lon": round(wgs.lon, 4), "crs": "wgs84"}},
    )
    assert as_gcj == as_wgs


# --- consent arbitration (DH-1 / P5) ----------------------------------------


def test_arbiter_denies_everything_before_plan_consent():
    arbiter = risk.Arbiter(build_default_registry())
    verdict = arbiter.decide(capability_id="clock.today", plan_consented=False, consent_ref=None)
    assert verdict.decision is risk.Decision.DENY
    assert verdict.reason == "ILLEGAL_PRE_CONSENT"


def test_arbiter_escalates_requirements_with_risk_level():
    arbiter = risk.Arbiter(build_default_registry())

    allowed = arbiter.decide(capability_id="clock.today", plan_consented=True, consent_ref="c-1")
    assert allowed.decision is risk.Decision.ALLOW

    l1 = arbiter.decide(capability_id="browser.click", plan_consented=True, consent_ref="c-1")
    assert l1.reason == "MISSING_ACTION_CONSENT"

    l2 = arbiter.decide(
        capability_id="booking.order.submit",
        plan_consented=True,
        consent_ref="c-1",
        has_action_consent=True,
    )
    assert l2.reason == "MISSING_CONFIRM_TOKEN"

    l2_ok = arbiter.decide(
        capability_id="booking.order.submit",
        plan_consented=True,
        consent_ref="c-1",
        has_action_consent=True,
        confirm_token_ok=True,
    )
    assert l2_ok.decision is risk.Decision.ALLOW


def test_allow_without_a_consent_reference_is_refused():
    arbiter = risk.Arbiter(build_default_registry())
    verdict = arbiter.decide(capability_id="clock.today", plan_consented=True, consent_ref=None)
    assert verdict.decision is risk.Decision.DENY
    assert verdict.reason == "MISSING_CONSENT_REF"


def test_payment_is_unreachable_by_absence_not_by_rule():
    arbiter = risk.Arbiter(build_default_registry())
    with pytest.raises(CapabilityNotFound):
        arbiter.decide(
            capability_id="payment.pay",
            plan_consented=True,
            consent_ref="c-1",
            has_action_consent=True,
            confirm_token_ok=True,
        )


# --- scoring (§6.3) ---------------------------------------------------------


def test_score_follows_the_framework_formula():
    breakdown = scoring.score(
        features={"f_time": 0.25},
        weights={"f_time": 1.0},
        penalties=[],
        uncertainty=DegradationLevel.D0,
    )
    assert breakdown.total == pytest.approx(0.75)
    assert breakdown.missing == []
    assert breakdown.uncertainty_penalty == pytest.approx(0.0)


def test_missing_feature_renormalises_weights_and_raises_uncertainty():
    breakdown = scoring.score(
        features={"f_time": 0.0, "f_cost": None},
        weights={"f_time": 0.5, "f_cost": 0.5},
        penalties=[],
        uncertainty=DegradationLevel.D0,
    )
    # f_cost is gone, so f_time carries the whole weighted sum rather than
    # half of it, and the candidate is penalised for the gap it now has.
    assert breakdown.contributions == {"f_time": pytest.approx(1.0)}
    assert breakdown.missing == ["f_cost"]
    assert breakdown.uncertainty_penalty == pytest.approx(0.15 * 0.30)
    assert breakdown.total == pytest.approx(1.0 - 0.045)


def test_weather_penalties_and_uncertainty_subtract_from_the_score():
    breakdown = scoring.score(
        features={"f_time": 0.0},
        weights={"f_time": 0.0},
        penalties=[0.5],
        uncertainty=DegradationLevel.D1,
    )
    assert breakdown.total == pytest.approx(-0.5 - 0.15 * 0.10)


def test_uncertainty_never_rewards_a_degraded_candidate():
    live = scoring.score(
        features={"f_time": 0.2},
        weights={"f_time": 1.0},
        penalties=[],
        uncertainty=DegradationLevel.D0,
    )
    deep_link = scoring.score(
        features={"f_time": 0.2},
        weights={"f_time": 1.0},
        penalties=[],
        uncertainty=DegradationLevel.D3,
    )
    assert live.total > deep_link.total


# --- weather two-path rule (§6.2) -------------------------------------------


def test_thunderstorm_vetoes_outdoor_plans_instead_of_scoring_them():
    assessment = weather_rules.assess({"thunderstorm": True}, modes=["walk"])
    assert assessment.penalties == []
    assert any("thunderstorm" in veto for veto in assessment.vetoes)
    assert all(factor.hard_veto for factor in assessment.factors)


def test_ordinary_rain_is_only_a_penalty_when_it_cannot_endanger_the_mode():
    assessment = weather_rules.assess({"precip_mm": 10.0}, modes=["train"])
    assert assessment.vetoes == []
    assert assessment.penalties == [pytest.approx(0.2)]


def test_heavy_rain_vetoes_driving():
    assessment = weather_rules.assess({"precipitation": "暴雨"}, modes=["drive"])
    assert any("precipitation" in veto for veto in assessment.vetoes)
    assert assessment.penalties == []


def test_empty_weather_is_neutral():
    assessment = weather_rules.assess({}, modes=["metro"])
    assert assessment.factors == []
    assert assessment.vetoes == []
    assert assessment.penalties == []


# --- reason traces (§6.4) ---------------------------------------------------


def test_reason_trace_is_explainable_and_only_cites_the_top_k():
    breakdown = scoring.score(
        features={"f_time": 0.1, "f_cost": 0.9},
        weights={"f_time": 0.5, "f_cost": 0.5},
        penalties=[],
        uncertainty=DegradationLevel.D0,
    )
    weather = weather_rules.assess({"precip_mm": 5.0}, modes=["walk"])
    trace = reason_trace.build(
        "cand-1",
        breakdown,
        weather,
        evidence={"f_time": {"source": "route.plan"}},
    )

    assert reason_trace.is_explainable(trace)
    top = reason_trace.citable(trace, 2)
    assert len(top) == 2
    assert abs(top[0].contribution) >= abs(top[1].contribution)
    assert "cand-1" in reason_trace.render_hint(trace)


def test_trace_without_a_preference_or_weather_signal_is_not_explainable():
    breakdown = scoring.score(
        features={},
        weights={"f_time": 1.0},
        penalties=[],
        uncertainty=DegradationLevel.D0,
    )
    trace = reason_trace.build("cand-2", breakdown, None, evidence={})
    assert not reason_trace.is_explainable(trace)
    assert "未被淘汰" in reason_trace.render_hint(trace)


# --- cross-leg constraints (§6.5) -------------------------------------------


def test_mixed_crs_is_a_bug_not_a_degradation():
    legs = [
        {
            "origin": {"lat": 39.9, "lon": 116.4, "crs": "gcj02"},
            "destination": {"lat": 40.4, "lon": 116.0, "crs": "wgs84"},
        }
    ]
    with pytest.raises(CoordinateSystemMismatch):
        validate_trip(legs, constraints={})


def test_short_connection_is_reported_as_a_recoverable_violation():
    start = datetime(2026, 9, 12, 8, 0, tzinfo=BEIJING)
    legs = [
        {"mode": "flight", "depart": start, "arrive": start + timedelta(minutes=120)},
        {
            "mode": "metro",
            "depart": start + timedelta(minutes=150),
            "arrive": start + timedelta(minutes=180),
        },
    ]
    violations = validate_trip(legs, constraints={})
    shortfalls = [item for item in violations if item.kind == "buffer_shortfall"]
    assert shortfalls and all(item.recoverable for item in shortfalls)
    assert shortfalls[0].leg_index == 1


def test_normal_connection_produces_no_violation():
    start = datetime(2026, 9, 12, 8, 0, tzinfo=BEIJING)
    legs = [
        {"mode": "train", "depart": start, "arrive": start + timedelta(minutes=60)},
        {
            "mode": "metro",
            "depart": start + timedelta(minutes=150),
            "arrive": start + timedelta(minutes=180),
        },
    ]
    assert validate_trip(legs, constraints={}) == []


def test_budget_overrun_is_soft_unless_the_user_set_a_hard_cap():
    legs = [{"mode": "metro", "price": 900.0}]
    soft = [item for item in validate_trip(legs, constraints={"budget_total": 500}) if item.kind == "budget"]
    hard = [
        item
        for item in validate_trip(legs, constraints={"budget_total": 500, "budget_hard": True})
        if item.kind == "budget"
    ]
    assert [item.recoverable for item in soft] == [True]
    assert [item.recoverable for item in hard] == [False]


def test_departure_window_is_enforced_against_clock_times():
    start = datetime(2026, 9, 12, 6, 30, tzinfo=BEIJING)
    legs = [{"mode": "train", "depart": start, "arrive": start + timedelta(minutes=60)}]
    violations = validate_trip(legs, constraints={"depart_window": {"earliest": "07:00"}})
    assert [item.kind for item in violations] == ["depart_window"]
