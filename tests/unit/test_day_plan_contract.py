"""Day planning contract (content layer F1: multi-day + multi-city + lodging).

The plan the user consents to is built by these functions, so the properties they
must hold are asserted here rather than left to the integration tests: how many
days, which city, where the transfer lands, what fits in a day, and what the
price does and does not claim to include.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.domain.geo import Coord, Crs
from app.domain.trip import DayAttraction, DayPlan, DayScheduleItem, TripSegment
from app.policy import day_plan


def _coord(lat: float, lon: float) -> Coord:
    return Coord(lat=lat, lon=lon, crs=Crs.WGS84)


def _segment(city: str, days: int, *, coord: Coord | None = None, area: str | None = None) -> TripSegment:
    return TripSegment(
        segment_id=f"seg_{city}", city=city, day_count=days, coord=coord, lodging_area=area
    )


def _attraction(name: str, *, coord: Coord | None = None, category: str = "museum", **kwargs) -> DayAttraction:
    return DayAttraction(
        attraction_id=f"attr_{name}",
        name=name,
        category=category,
        coord=coord,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Windows and segment assignment
# ---------------------------------------------------------------------------


def test_day_windows_cover_every_calendar_day() -> None:
    windows = day_plan.make_day_windows(date(2026, 10, 1), 3)

    assert [window.date for window in windows] == [
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
    ]
    assert [window.day_index for window in windows] == [1, 2, 3]


def test_reversed_rhythm_falls_back_to_default_window() -> None:
    start, end = day_plan.parse_rhythm({"start": "20:00", "end": "09:00"})

    assert (start, end) == (day_plan.time(9, 0), day_plan.time(20, 0))


def test_rhythm_is_honoured() -> None:
    windows = day_plan.make_day_windows(date(2026, 10, 1), 1, rhythm={"start": "07:30", "end": "18:00"})

    assert windows[0].start == day_plan.time(7, 30)
    assert windows[0].end == day_plan.time(18, 0)
    assert windows[0].capacity_minutes == 630


def test_multi_city_days_are_split_in_order() -> None:
    segments = [_segment("北京", 3), _segment("天津", 2)]
    windows = day_plan.assign_days_to_segments(segments, day_plan.make_day_windows(date(2026, 10, 1), 5))

    assert [window.city for window in windows] == ["北京", "北京", "北京", "天津", "天津"]


def test_transfer_lands_on_the_last_day_of_the_departing_city() -> None:
    # 4 content days + 1 transfer day = 5 calendar days.
    segments = [_segment("北京", 2), _segment("天津", 1), _segment("承德", 1)]
    segments[1].transfer_in = {"leg_id": "leg_1", "modes": ["train"]}
    segments[2].transfer_in = {"leg_id": "leg_2", "modes": ["train"]}
    windows = day_plan.assign_days_to_segments(
        segments,
        day_plan.make_day_windows(date(2026, 10, 1), 5),
        transfer_days=1,
    )

    assert [window.is_transfer_day for window in windows] == [False, False, True, False, False]
    # The transfer day is spent leaving one city and sleeping in the next: the
    # activities are still the departing city's.
    assert [window.city for window in windows] == ["北京", "北京", "北京", "天津", "承德"]
    assert [window.overnight_city for window in windows] == ["北京", "北京", "天津", "天津", "承德"]


def test_two_transfer_days_are_placed_one_per_hop() -> None:
    segments = [_segment("北京", 1), _segment("天津", 1), _segment("承德", 1)]
    segments[1].transfer_in = {"leg_id": "leg_1"}
    segments[2].transfer_in = {"leg_id": "leg_2"}
    windows = day_plan.assign_days_to_segments(
        segments,
        day_plan.make_day_windows(date(2026, 10, 1), 5),
        transfer_days=2,
    )

    assert [window.is_transfer_day for window in windows] == [False, True, False, True, False]
    assert [window.overnight_city for window in windows] == ["北京", "天津", "天津", "承德", "承德"]


def test_no_reserved_transfer_day_makes_the_move_share_a_day() -> None:
    """When the day counts already fill the trip, the move is not a separate day."""

    segments = [_segment("北京", 3), _segment("天津", 2)]
    segments[1].transfer_in = {"leg_id": "leg_1"}
    windows = day_plan.assign_days_to_segments(
        segments, day_plan.make_day_windows(date(2026, 10, 1), 5)
    )

    assert [window.is_transfer_day for window in windows] == [False, False, False, False, False]
    assert [window.city for window in windows] == ["北京", "北京", "北京", "天津", "天津"]


def test_transfer_day_is_flagged_and_carries_the_leg() -> None:
    segments = [_segment("北京", 1), _segment("天津", 2)]
    segments[1].transfer_in = {"leg_id": "leg_x", "duration_min": 40, "modes": ["train"]}
    windows = day_plan.assign_days_to_segments(
        segments,
        day_plan.make_day_windows(date(2026, 10, 1), 3),
        transfer_days=1,
    )

    assert windows[1].is_transfer_day is True
    assert windows[1].city == "北京"
    assert windows[1].overnight_city == "天津"
    assert (windows[1].transfer or {}).get("leg_id") == "leg_x"
    assert windows[0].is_transfer_day is False
    assert windows[2].is_transfer_day is False


# ---------------------------------------------------------------------------
# Attraction assignment
# ---------------------------------------------------------------------------


def test_unstated_days_keep_a_clean_split_even() -> None:
    """3:2 over five windows must not become 4:1 just because of list order."""

    assert day_plan._distribute([3, 2], 5) == [3, 2]
    assert day_plan._distribute([1, 1], 5) == [3, 2]
    assert day_plan._distribute([1, 1, 1], 4) == [2, 1, 1]
    assert day_plan._distribute([1, 1], 2) == [1, 1]
    assert day_plan._distribute([1, 1, 1], 2) == [1, 1, 0]


def test_attractions_are_clustered_by_day_not_stacked() -> None:
    # Two tight clusters ~50 km apart in a city, two days available.
    pool = [
        _attraction("a1", coord=_coord(39.90, 116.40)),
        _attraction("a2", coord=_coord(39.905, 116.405)),
        _attraction("b1", coord=_coord(40.35, 116.85)),
        _attraction("b2", coord=_coord(40.355, 116.855)),
    ]
    windows = day_plan.make_day_windows(date(2026, 10, 1), 2)
    anchors = {1: _coord(39.90, 116.40)}
    assignment, unassigned, notes = day_plan.assign_attractions(
        pool, windows, anchors=anchors, per_day_max=2
    )

    first = {item.attraction_id for item in assignment[1]}
    second = {item.attraction_id for item in assignment[2]}
    assert first == {"attr_a1", "attr_a2"}, "day 1 must stay in the anchor cluster"
    assert second == {"attr_b1", "attr_b2"}
    assert unassigned == []
    assert notes == []


def test_unassigned_attractions_are_reported_not_dropped() -> None:
    pool = [_attraction(f"x{index}", coord=_coord(39.9 + index * 0.01, 116.4)) for index in range(6)]
    windows = day_plan.make_day_windows(date(2026, 10, 1), 1)
    assignment, unassigned, notes = day_plan.assign_attractions(pool, windows, per_day_max=2)

    assert len(assignment[1]) == 2
    assert len(unassigned) == 4
    assert any("备选" in note for note in notes)


def test_attractions_without_coordinates_are_reported() -> None:
    pool = [_attraction("no_coord"), _attraction("ok", coord=_coord(39.9, 116.4))]
    windows = day_plan.make_day_windows(date(2026, 10, 1), 1)
    _, unassigned, notes = day_plan.assign_attractions(pool, windows)

    assert [item.name for item in unassigned] == ["no_coord"]
    assert any("没有坐标" in note for note in notes)


# ---------------------------------------------------------------------------
# Packing a day
# ---------------------------------------------------------------------------


def test_pack_day_places_stops_within_the_window() -> None:
    window = day_plan.make_day_windows(date(2026, 10, 1), 1)[0].model_copy(update={"city": "北京"})
    stops = [
        _attraction("故宫", coord=_coord(39.9163, 116.3972), recommend_duration_min=180),
        _attraction("国博", coord=_coord(39.9055, 116.3976), recommend_duration_min=120),
    ]
    day = day_plan.pack_day(window, stops, transfers={("attr_故宫", "attr_国博"): 25})

    assert [item.start.hour for item in day.items] == [9, 12]
    assert all(item.end <= datetime(2026, 10, 1, 20, 0) for item in day.items if item.end)
    assert day.items[0].transport_to_next == {"duration_min": 25, "estimated": False}


def test_pack_day_reports_what_did_not_fit() -> None:
    window = day_plan.make_day_windows(date(2026, 10, 1), 1)[0]
    stops = [
        _attraction(f"点{index}", coord=_coord(39.9 + index * 0.01, 116.4), recommend_duration_min=240)
        for index in range(4)
    ]
    day = day_plan.pack_day(window, stops)

    assert len(day.attractions) == 2, "4 x 4h cannot fit in an 11h window"
    assert any("时间不足" in note for note in day.notes)
    assert any("移至其它日期" in note for note in day.notes)


def test_pack_day_starts_after_a_transfer_arrival() -> None:
    window = day_plan.make_day_windows(date(2026, 10, 1), 2)[0]
    window = window.model_copy(
        update={
            "is_transfer_day": True,
            "city": "天津",
            "transfer": {
                "leg_id": "leg_1",
                "depart": "2026-10-01T06:08:00+08:00",
                "arrive": "2026-10-01T16:10:00+08:00",
                "duration_min": 40,
                "modes": ["train"],
            },
        }
    )
    stop = _attraction("意风区", coord=_coord(39.14, 117.21), recommend_duration_min=120)
    day = day_plan.pack_day(window, [stop])

    assert day.items[0].kind == "city_transfer"
    assert day.items[1].start.hour >= 16
    assert any("到达较晚" in note for note in day.notes)


def test_transfer_day_without_a_leg_says_so() -> None:
    window = day_plan.make_day_windows(date(2026, 10, 1), 1)[0].model_copy(
        update={"is_transfer_day": True, "transfer": None}
    )
    day = day_plan.pack_day(window, [])

    assert any("未获取到具体车次" in note for note in day.notes)


def test_pack_day_appends_meals_and_lodging() -> None:
    window = day_plan.make_day_windows(date(2026, 10, 1), 1)[0].model_copy(update={"city": "北京"})
    day = day_plan.pack_day(
        window,
        [_attraction("故宫", coord=_coord(39.9163, 116.3972), recommend_duration_min=120)],
        lodging=day_plan.LodgingOption(name="王府井一带", advisory=True),
        meals=day_plan.default_meals("北京"),
    )

    kinds = [item.kind for item in day.items]
    assert kinds == ["attraction", "meal", "meal", "lodging"]
    assert day.lodging[0].name == "王府井一带"


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def test_thunderstorm_replaces_outdoor_stops_with_indoor_ones() -> None:
    day = DayPlan(
        day_index=1,
        date=date(2026, 10, 1),
        city="北京",
        attractions=[_attraction("长城", category="historic", coord=_coord(40.35, 116.0))],
    )
    adjusted = day_plan.adjust_for_weather(
        day,
        {"thunderstorm": True},
        indoor_pool=[_attraction("国博", category="museum", coord=_coord(39.9, 116.4))],
    )

    assert adjusted.weather_veto is True
    assert adjusted.attractions[0].name == "国博"
    assert any("替换" in note for note in adjusted.notes)


def test_weather_veto_without_an_alternative_is_flagged_not_dropped() -> None:
    day = DayPlan(
        day_index=1,
        date=date(2026, 10, 1),
        city="北京",
        attractions=[_attraction("长城", category="historic", coord=_coord(40.35, 116.0))],
    )
    adjusted = day_plan.adjust_for_weather(day, {"thunderstorm": True})

    assert adjusted.weather_veto is True
    assert adjusted.attractions[0].name == "长城"
    assert any("没有可替换" in note for note in adjusted.notes)


def test_calm_weather_does_not_veto() -> None:
    day = DayPlan(day_index=1, date=date(2026, 10, 1), city="北京",
                  attractions=[_attraction("故宫", category="museum", coord=_coord(39.9, 116.4))])
    adjusted = day_plan.adjust_for_weather(day, {"temperature_c": 22.0, "precipitation_mm": 0.0})

    assert adjusted.weather_veto is False
    assert adjusted.attractions[0].name == "故宫"


# ---------------------------------------------------------------------------
# Lodging and price
# ---------------------------------------------------------------------------


def test_lodging_follows_the_city_you_sleep_in() -> None:
    segments = [_segment("北京", 1, area="王府井", coord=_coord(39.91, 116.41)), _segment("天津", 1)]
    segments[1].transfer_in = {"leg_id": "leg_1"}
    windows = day_plan.assign_days_to_segments(
        segments, day_plan.make_day_windows(date(2026, 10, 1), 3), transfer_days=1
    )
    options = day_plan.build_lodging_options(segments, windows)

    assert options[1].area == "王府井"
    assert options[2].area == "天津", "the transfer day sleeps in the arrival city"
    assert options[3].area == "天津"


def test_anchors_come_from_the_segment_stay_location() -> None:
    segments = [_segment("北京", 2, coord=_coord(39.91, 116.41))]
    windows = day_plan.assign_days_to_segments(segments, day_plan.make_day_windows(date(2026, 10, 1), 2))
    anchors = day_plan.anchors_from_segments(segments, windows)

    assert anchors[1] is not None
    assert anchors[2] is not None


def test_price_estimate_sums_known_items_and_names_the_gaps() -> None:
    days = [
        DayPlan(
            day_index=1,
            date=date(2026, 10, 1),
            attractions=[_attraction("故宫", ticket_price=60.0), _attraction("免费公园", ticket_price=0.0)],
            lodging=[day_plan.LodgingOption(name="某酒店", nightly_price=480.0)],
        )
    ]
    total, note = day_plan.price_estimate(days, [_segment("北京", 1)])

    assert total == pytest.approx(540.0)
    assert "门票" in note and "住宿" in note
    assert "未含" in note


def test_price_estimate_is_unknown_rather_than_zero_when_nothing_is_priced() -> None:
    days = [DayPlan(day_index=1, date=date(2026, 10, 1), attractions=[_attraction("未知")])]
    total, note = day_plan.price_estimate(days, [_segment("北京", 1)])

    assert total is None, "a fabricated 0 would read as 'free'"
    assert "未含" in note


def test_transfer_minutes_estimate_is_labelled() -> None:
    minutes = day_plan.estimate_transfer_minutes(_coord(39.9, 116.4), _coord(39.95, 116.45))

    assert minutes is not None and minutes > 15


def test_transfer_minutes_missing_coordinates_returns_none() -> None:
    assert day_plan.estimate_transfer_minutes(None, _coord(39.9, 116.4)) is None
