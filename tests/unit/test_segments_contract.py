"""Segment parsing and day allocation contract (content layer F1, D3).

These are decisions, not prose: the whole itinerary is shaped by them, so they
are pinned by tests rather than by hoping the LLM extracts them consistently.
"""

from __future__ import annotations

import pytest

from app.policy import segments as seg


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("北京3天+天津2天", [("北京", 3), ("天津", 2)]),
        ("北京玩三天，然后去天津待两天", [("北京", 3), ("天津", 2)]),
        ("3天北京，2天天津", [("北京", 3), ("天津", 2)]),
        ("北京3天然后天津2天", [("北京", 3), ("天津", 2)]),
        ("北京3天，天津2天，承德1天", [("北京", 3), ("天津", 2), ("承德", 1)]),
        ("上海3天，苏州1天", [("上海", 3), ("苏州", 1)]),
        ("我想去北京5天，然后去上海3天，最后回北京2天", [("北京", 5), ("上海", 3), ("北京", 2)]),
        ("上海5天", [("上海", 5)]),
        ("北京玩3天", [("北京", 3)]),
        ("北京两天", [("北京", 2)]),
        ("重庆2晚", [("重庆", 2)]),
        ("10天云南", [("云南", 10)]),
        ("日本东京5天", [("日本东京", 5)]),
        ("三亚5天", [("三亚", 5)]),
        ("天津2天", [("天津", 2)]),
        # "从北京出发去天津" names the destination, not the origin.
        ("从北京出发去天津玩2天", [("天津", 2)]),
        # Prose: the duration is separated from its city by punctuation.
        ("想去日本东京，5天，2人，赏樱之旅", [("日本东京", 5)]),
        ("丽江大理，7天，2人，休闲游", [("丽江大理", 7)]),
    ],
)
def test_parse_pairs(text: str, expected: list[tuple[str, int]]) -> None:
    parsed = seg.parse_segments(text)
    assert [(s.city, s.days) for s in parsed.segments] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("北京，天津", ["北京", "天津"]),
        ("北京、天津、承德", ["北京", "天津", "承德"]),
        ("成都", ["成都"]),
    ],
)
def test_parse_without_durations_keeps_cities(text: str, expected: list[str]) -> None:
    parsed = seg.parse_segments(text)
    assert [s.city for s in parsed.segments] == expected
    assert all(s.days is None for s in parsed.segments)


def test_parse_segments_caps_count_and_says_so() -> None:
    parsed = seg.parse_segments("北京1天，天津1天，承德1天，秦皇岛1天，唐山1天")
    assert len(parsed.segments) == seg.MAX_SEGMENTS
    assert any("上限" in note for note in parsed.notes)


def test_parse_empty_text_yields_nothing() -> None:
    for text in ("", "   "):
        parsed = seg.parse_segments(text)
        assert parsed.segments == []


def test_unparseable_text_becomes_one_segment_and_is_reported() -> None:
    parsed = seg.parse_segments("?!?!")
    assert len(parsed.segments) == 1
    assert parsed.notes, "an unparsed request must be reported, never silently reshaped"


@pytest.mark.parametrize(
    ("text", "value"),
    [("3", 3), ("三", 3), ("十二", 12), ("两", 2), ("10", 10), ("十", 10), ("", None), ("abc", None)],
)
def test_cn_to_int(text: str, value: int | None) -> None:
    assert seg.cn_to_int(text) == value


# ---------------------------------------------------------------------------
# Day allocation: the reading of "北京3天 + 天津2天" depends on the trip length.
# ---------------------------------------------------------------------------


def test_explicit_counts_summing_to_trip_days_are_calendar_days() -> None:
    parsed = seg.parse_segments("北京3天+天津2天")
    notes: list[str] = []
    planned, transfer_days = seg.allocate_days(parsed.segments, 5, notes=notes)

    assert [(p.city, p.days) for p in planned] == [("北京", 3), ("天津", 2)]
    assert transfer_days == 0, "the transfer day is already one of the five"
    assert notes == [], "a consistent request must not be re-scaled"


def test_explicit_counts_shorter_than_trip_days_reserve_a_transfer_day() -> None:
    parsed = seg.parse_segments("北京3天+天津2天")
    notes: list[str] = []
    planned, transfer_days = seg.allocate_days(parsed.segments, 4, notes=notes)

    assert transfer_days == 1
    assert sum(p.days for p in planned) + transfer_days == 4
    assert any("超过可安排天数" in note for note in notes)


def test_unknown_days_are_spread_evenly() -> None:
    parsed = seg.parse_segments("北京，天津")
    planned, transfer_days = seg.allocate_days(parsed.segments, 5)

    assert transfer_days == 1
    assert sum(p.days for p in planned) == 4
    assert all(p.days >= 1 for p in planned)


def test_three_cities_reserve_two_transfer_days() -> None:
    parsed = seg.parse_segments("北京、天津、承德")
    planned, transfer_days = seg.allocate_days(parsed.segments, 6)

    assert transfer_days == 2
    assert sum(p.days for p in planned) == 4


def test_too_few_days_drops_segments_and_reports_it() -> None:
    parsed = seg.parse_segments("北京3天+天津2天")
    notes: list[str] = []
    planned, _ = seg.allocate_days(parsed.segments, 1, notes=notes)

    assert len(planned) == 1
    assert notes, "dropping a destination must be visible in the assumptions"


def test_single_city_needs_no_transfer_day() -> None:
    parsed = seg.parse_segments("上海5天")
    planned, transfer_days = seg.allocate_days(parsed.segments, 5)

    assert transfer_days == 0
    assert [(p.city, p.days) for p in planned] == [("上海", 5)]


def test_allocation_assigns_stable_segment_ids() -> None:
    parsed = seg.parse_segments("北京3天+天津2天")
    planned, _ = seg.allocate_days(parsed.segments, 5)

    ids = [p.segment_id for p in planned]
    assert len(set(ids)) == len(ids)
    assert all(ids)
