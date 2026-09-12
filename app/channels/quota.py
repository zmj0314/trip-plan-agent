"""Quota accounting (framework §4.1.4, §6.10.5).

The pools are wildly uneven -- amap gives 150k basic-LBS calls a month but only
**5,000 searches** -- so "how much is left?" has to be answered by counted
state, not by a guess made after the fact.

Reserve-then-settle is what makes the count honest: a call that is in progress
is already counted, so two concurrent calls cannot both believe the last unit is
available. A failed call rolls its reservation back, and a repeated ``idem``
(the same logical call retried) does not count twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from app.domain.timebase import now_local
from app.store.repositories import QuotaRepo


class QuotaState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class QuotaDecision(StrEnum):
    OK = "ok"
    WARN = "warn"
    DEGRADE = "degrade"
    EXHAUSTED = "exhausted"


def window_key(moment: datetime | None = None) -> str:
    """Monthly buckets, the granularity every free tier in the plan uses."""

    return (moment or now_local()).strftime("%Y-%m")


class QuotaLedger:
    def __init__(
        self,
        db,
        *,
        limits: Mapping[str, int],
        warn_ratio: float = 0.80,
        degrade_ratio: float = 0.95,
        repo: QuotaRepo | None = None,
    ) -> None:
        self._repo = repo or QuotaRepo(db)
        self._limits = {str(pool): int(limit) for pool, limit in dict(limits).items()}
        self._warn_ratio = float(warn_ratio)
        self._degrade_ratio = float(degrade_ratio)

    # ------------------------------------------------------------------ write
    def reserve(self, pool: str, units: int, *, idem: str, window: str | None = None) -> QuotaDecision:
        """Count ``units`` against ``pool`` before the call is made.

        Idempotent per ``idem``: a retry of the same logical call reports the
        current state without charging the pool a second time.
        """

        bucket = window or window_key()
        units = max(0, int(units))
        limit = self._limits.get(str(pool))

        if self._repo.get(idem) is not None:
            return self._classify(self._repo.used(str(pool), bucket), limit)

        used = self._repo.used(str(pool), bucket)
        if limit is not None and limit >= 0 and used + units > limit:
            # Exhausted: deliberately *not* recorded, so the pool does not grow
            # by failed attempts and a later window starts clean.
            return QuotaDecision.EXHAUSTED

        self._repo.reserve(idem=idem, pool_id=str(pool), window_key=bucket, units=units)
        return self._classify(used + units, limit)

    def commit(self, idem: str) -> None:
        self._repo.set_state(idem, QuotaState.COMMITTED.value)

    def rollback(self, idem: str) -> None:
        self._repo.set_state(idem, QuotaState.ROLLED_BACK.value)

    # ------------------------------------------------------------------- read
    def used(self, pool: str, *, window: str | None = None) -> int:
        return self._repo.used(str(pool), window or window_key())

    def limit(self, pool: str) -> int | None:
        return self._limits.get(str(pool))

    def remaining(self, pool: str, *, window: str | None = None) -> int | None:
        limit = self._limits.get(str(pool))
        if limit is None:
            return None
        return max(0, limit - self.used(pool, window=window))

    # ---------------------------------------------------------------- private
    def _classify(self, used: int, limit: int | None) -> QuotaDecision:
        if limit is None or limit <= 0:
            return QuotaDecision.OK
        ratio = used / limit
        if ratio >= self._degrade_ratio:
            return QuotaDecision.DEGRADE
        if ratio >= self._warn_ratio:
            return QuotaDecision.WARN
        return QuotaDecision.OK


__all__ = ["QuotaDecision", "QuotaLedger", "QuotaState", "window_key"]
