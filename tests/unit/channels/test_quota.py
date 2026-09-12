"""Framework §3.5 / §4.1.4: reserve, settle, and never double-charge."""

from __future__ import annotations

from app.channels.quota import QuotaDecision, QuotaLedger, window_key
from tests.unit.channels.fakes import memory_db


def ledger(**limits) -> QuotaLedger:
    return QuotaLedger(memory_db(), limits=limits or {"amap_search": 5000}, warn_ratio=0.8, degrade_ratio=0.95)


def test_reserving_the_same_idem_twice_counts_once():
    quotas = ledger()
    assert quotas.reserve("amap_search", 1, idem="q1") is QuotaDecision.OK
    assert quotas.reserve("amap_search", 1, idem="q1") is QuotaDecision.OK
    assert quotas.used("amap_search") == 1


def test_committed_and_reserved_both_count_as_used():
    quotas = ledger()
    quotas.reserve("amap_search", 1, idem="q1")
    quotas.reserve("amap_search", 1, idem="q2")
    quotas.commit("q1")

    assert quotas.used("amap_search") == 2
    assert quotas.remaining("amap_search") == 4998


def test_rolling_back_frees_the_unit():
    quotas = ledger()
    quotas.reserve("amap_search", 1, idem="q1")
    quotas.rollback("q1")

    assert quotas.used("amap_search") == 0


def test_thresholds_report_warn_then_degrade_then_exhaustion():
    quotas = QuotaLedger(memory_db(), limits={"amap_search": 10}, warn_ratio=0.8, degrade_ratio=0.95)

    decisions = [quotas.reserve("amap_search", 1, idem=f"q{n}") for n in range(10)]
    assert decisions[0] is QuotaDecision.OK
    assert decisions[7] is QuotaDecision.WARN
    assert decisions[9] is QuotaDecision.DEGRADE

    assert quotas.reserve("amap_search", 1, idem="overflow") is QuotaDecision.EXHAUSTED
    assert quotas.used("amap_search") == 10


def test_unknown_pools_are_unmetered():
    quotas = ledger()
    assert quotas.reserve("not_a_pool", 999, idem="q1") is QuotaDecision.OK
    assert quotas.remaining("not_a_pool") is None


def test_windows_are_counted_separately():
    quotas = ledger()
    quotas.reserve("amap_search", 1, idem="q1", window="2026-09")
    assert quotas.used("amap_search", window="2026-09") == 1
    assert quotas.used("amap_search", window="2026-10") == 0
    assert window_key().count("-") == 1
