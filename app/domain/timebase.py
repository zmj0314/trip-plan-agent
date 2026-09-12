"""Date and time normalisation.

Framework §11: dates are ``YYYY-MM-DD``, and relative dates must be resolved
against the authoritative "today" (``clock.today``) rather than the model's
guess.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DATE_FMT = "%Y-%m-%d"


def _local_tz() -> timezone | ZoneInfo:
    """Asia/Shanghai, without requiring the ``tzdata`` wheel on Windows.

    Mainland China has had no DST since 1991, so a fixed +08:00 offset is
    exactly equivalent year-round.
    """

    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "Asia/Shanghai")


LOCAL_TZ = _local_tz()


def to_ymd(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return parse_ymd(value).strftime(DATE_FMT)
    if isinstance(value, datetime):
        return value.date().strftime(DATE_FMT)
    return value.strftime(DATE_FMT)


def parse_ymd(value: date | datetime | str) -> date:
    """Parse a date from the handful of shapes that reach the domain layer.

    A real ``date``/``datetime`` is accepted and returned as-is: the stored slot
    value may already have been normalised, and requiring every caller to check
    the type first is how a `.strip()` on a date ends up in a code path nobody
    exercised.
    """

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in (DATE_FMT, "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r} (expected YYYY-MM-DD)")


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def combine_ymd_hm(day: date | str, hhmm: str) -> datetime:
    """Combine a date with an ``HH:MM`` string into a local datetime."""

    d = parse_ymd(day) if isinstance(day, str) else day
    hour, _, minute = hhmm.partition(":")
    return datetime.combine(d, time(int(hour), int(minute)), tzinfo=LOCAL_TZ)


def add_minutes(moment: datetime, minutes: int) -> datetime:
    return moment + timedelta(minutes=minutes)


def minutes_between(earlier: datetime, later: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)
