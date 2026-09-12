"""Offline export and share (L0, computed in-process).

These are the two deliverables the user owns: a file they can print, and a
snapshot they can send. Both must work with no key and no network, which is why
they live in the local channel rather than behind a service.
"""

from __future__ import annotations

from app.channels.local import EXPORT_FORMATS, build_share, export_plan

PLAN = {
    "legs": [
        {
            "origin_text": "北京南",
            "destination_text": "天津",
            "depart": "2026-10-03T06:08:00",
            "arrive": "2026-10-03T06:40:00",
            "train_code": "C2001",
        }
    ],
    "segments": [{"city": "北京", "day_count": 3}, {"city": "天津", "day_count": 2}],
    "days": [
        {
            "day_index": 1,
            "date": "2026-10-01",
            "city": "北京",
            "theme": "城区漫步",
            "attractions": [{"attraction_id": "a1", "name": "故宫", "ticket_price": 60.0}],
            "items": [
                {
                    "kind": "attraction",
                    "ref_id": "a1",
                    "start": "2026-10-01T09:00:00",
                    "end": "2026-10-01T12:00:00",
                }
            ],
            "lodging": [{"area": "王府井", "nightly_price": 480.0}],
        },
        {
            "day_index": 3,
            "date": "2026-10-03",
            "city": "北京",
            "is_transfer_day": True,
            "overnight_city": "天津",
            "items": [{"kind": "city_transfer", "evidence": ["北京南 → 天津"]}],
        },
    ],
    "content_notes": ["跨城日请预留取票时间"],
    "cost_note": "含门票 ¥60；未含市内交通与餐饮",
}


def test_markdown_export_contains_the_day_plan() -> None:
    out = export_plan({"plan": PLAN, "format": "markdown"})

    assert out["filename"] == "itinerary.md"
    assert out["media_type"].startswith("text/markdown")
    assert out["bytes"] > 0
    body = out["content"]
    assert "Day 1（2026-10-01）北京 · 城区漫步" in body
    assert "09:00–12:00  故宫（¥60 参考价）" in body, "times are trimmed to HH:MM"
    assert "今晚住 王府井（参考价 ¥480/晚）" in body
    assert "转场 北京南 → 天津" in body
    assert "C2001" in body
    assert "跨城日请预留取票时间" in body
    assert "费用：含门票 ¥60" in body


def test_transfer_day_is_visible_in_the_export() -> None:
    body = export_plan({"plan": PLAN})["content"]
    assert "转场" in body, "a moving day must not look like a free day in the file"


def test_html_export_escapes_the_content() -> None:
    out = export_plan({"plan": {**PLAN, "content_notes": ["a < b & c > d"]}, "format": "html"})

    assert out["filename"] == "itinerary.html"
    assert "&lt;" in out["content"] and "&amp;" in out["content"]
    assert "<b>" not in out["content"]


def test_unknown_format_is_reported_but_still_renders() -> None:
    """A caller asking for PDF gets markdown *and* is told that is what happened."""

    out = export_plan({"plan": PLAN, "format": "pdf"})
    assert out["format"] == "markdown"
    assert out["requested_format"] == "pdf"
    assert "pdf" not in out["supported_formats"], "the payload says what is actually on offer"
    assert out["filename"].endswith(".md")
    assert "故宫" in out["content"], "an unsupported format must still produce a file"


def test_export_of_an_empty_plan_is_not_an_error() -> None:
    out = export_plan({"plan": {}})
    assert out["content"].strip() == "行程单"


def test_export_accepts_a_nested_trip_wrapper() -> None:
    """A caller that hands over the whole snapshot must not get an empty file."""

    out = export_plan({"plan": {"trip": PLAN}})
    assert "故宫" in out["content"]


def test_share_is_a_self_contained_file() -> None:
    out = build_share({"plan": PLAN})

    assert out["mode"] == "file"
    assert out["filename"] == "itinerary.html"
    assert out["media_type"].startswith("text/html")
    assert "故宫" in out["content"]
    assert out["payment"] == "user_side"
    assert "不提供托管链接" in out["note"], "the honest limitation is part of the payload"


def test_export_and_share_are_free_of_side_effects() -> None:
    for out in (export_plan({"plan": PLAN}), build_share({"plan": PLAN})):
        assert out["payment"] == "user_side"
        assert "url" not in out or "http" not in str(out.get("url"))
