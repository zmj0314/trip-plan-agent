"""The 12306 read-only flow (framework §7 step ②).

The selectors in ``app/browser/sites.py`` are **unverified** -- written from the
documented page structure, never checked against the live site. These tests pin
what must happen in that situation: the flow must fail loudly and say the
selectors may have moved, rather than returning a table it did not find.
"""

from __future__ import annotations

import pytest

from app.browser.adapter import BrowserAdapter
from app.browser.contract import Expectation
from app.browser.sites import RAIL_SELECTORS, RailFlow, parse_rail_rows
from app.capabilities.contract import ResultStatus
from app.capabilities.registry import CapabilityBinding
from tests.unit.browser.fake_driver import FakeBrowserDriver, FakePage, reveal


def _binding(remote: str) -> CapabilityBinding:
    return CapabilityBinding(adapter_id="browser", remote_name=remote)


def _results_page(text: str) -> FakePage:
    page = FakePage(url="https://kyfw.12306.cn/otn/leftTicket/init", title="车票预订", text=text)
    page.selectors.add(RAIL_SELECTORS.get("result_table"))
    return page


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_train_codes_are_extracted_from_the_table_text() -> None:
    rows = parse_rail_rows("G1 北京南 上海虹桥 06:00 11:29\nD311 北京南 上海 20:05 07:12")
    assert [row["train_code"] for row in rows] == ["G1", "D311"]


def test_lines_without_a_train_code_are_ignored() -> None:
    """Column headers and stray text must not become train candidates."""

    assert parse_rail_rows("车次 出发站 到达站\n(无)") == []


def test_parsing_never_invents_columns() -> None:
    """Without a verified page the column order is unknown, so only the code is
    claimed -- the raw text travels with it."""

    row = parse_rail_rows("G1 次 06:00 11:29 ¥553")[0]
    assert set(row) == {"train_code", "text"}
    assert "¥553" in row["text"]


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


async def test_the_flow_fills_the_form_and_reads_the_table() -> None:
    driver = FakeBrowserDriver()
    driver.pages["https://kyfw.12306.cn/otn/leftTicket/init"] = _results_page("G1 次 有票")
    flow = RailFlow(driver, timeout_seconds=1.0)

    text = await flow.run(from_station="北京南", to_station="上海虹桥", date="2026-10-03")

    typed = [selector for name, selector in driver.calls if name == "type"]
    assert RAIL_SELECTORS.get("from_input") in typed
    assert RAIL_SELECTORS.get("date_input") in typed
    assert "有票" in text


async def test_the_flow_fails_loudly_when_the_results_table_never_appears() -> None:
    """The realistic failure: the site changed and the selectors are stale."""

    driver = FakeBrowserDriver()
    # A page with no results table at all.
    driver.pages["https://kyfw.12306.cn/otn/leftTicket/init"] = FakePage(
        url="https://kyfw.12306.cn/otn/leftTicket/init", text="请选择出发站"
    )
    driver.wait_for = _never  # type: ignore[assignment]
    flow = RailFlow(driver, timeout_seconds=0.2)

    from app.browser.contract import BrowserError

    with pytest.raises(BrowserError) as excinfo:
        await flow.run(from_station="北京南", to_station="上海虹桥", date="2026-10-03")
    assert "选择器可能已变更" in str(excinfo.value)
    assert excinfo.value.kind == "business"


async def test_the_flow_never_submits_an_order() -> None:
    """Read-only means read-only: no click except the query button."""

    driver = FakeBrowserDriver()
    driver.pages["https://kyfw.12306.cn/otn/leftTicket/init"] = _results_page("G1 次 有票")
    flow = RailFlow(driver, timeout_seconds=1.0)

    await flow.run(from_station="北京南", to_station="上海虹桥", date="2026-10-03")

    clicks = [selector for name, selector in driver.calls if name == "click"]
    assert clicks == [RAIL_SELECTORS.get("query_button")]


async def test_the_flow_declares_the_results_table_as_its_acceptance_marker() -> None:
    driver = FakeBrowserDriver()
    flow = RailFlow(driver)

    expectation = flow.expectation
    assert expectation.selector_present == RAIL_SELECTORS.get("result_table")
    assert expectation.url_contains == "12306"


# ---------------------------------------------------------------------------
# Through the capability surface
# ---------------------------------------------------------------------------


async def test_capability_returns_trains_and_says_the_selectors_are_unverified() -> None:
    driver = FakeBrowserDriver()
    driver.pages["https://kyfw.12306.cn/otn/leftTicket/init"] = _results_page(
        "G1 北京南 上海虹桥 06:00 11:29\nG3 北京南 上海虹桥 07:00 12:20"
    )
    adapter = BrowserAdapter(driver)

    result = await adapter.invoke(
        _binding("read_rail_availability"),
        {"fromStation": "北京南", "toStation": "上海虹桥", "date": "2026-10-03"},
        timeout=20.0,
    )

    assert result.ok is True
    assert result.data["rows"] == 2
    assert [train["train_code"] for train in result.data["trains"]] == ["G1", "G3"]
    assert result.data["selectors_verified"] is False, "an unchecked page must not claim otherwise"


async def test_capability_requires_the_three_parameters() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver())
    result = await adapter.invoke(
        _binding("read_rail_availability"), {"fromStation": "北京南"}, timeout=20.0
    )

    assert result.ok is False
    assert "toStation" in result.warnings[0] and "date" in result.warnings[0]


async def test_a_missing_browser_is_unavailable_not_error() -> None:
    """It must report *absent*, so the resolver falls through to the MCP search.

    Returning ERROR here would truncate the provider chain and turn "no browser"
    into "no rail data at all".
    """

    driver = FakeBrowserDriver(navigate_fails=True)
    adapter = BrowserAdapter(driver)
    result = await adapter.invoke(
        _binding("read_rail_availability"),
        {"fromStation": "北京南", "toStation": "上海虹桥", "date": "2026-10-03"},
        timeout=20.0,
    )

    assert result.status is ResultStatus.ERROR, "a network failure is not an absent provider"
    assert "network" in result.degradation.reason


async def test_the_reservation_flow_is_absent_rather_than_guessed() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver())
    result = await adapter.invoke(_binding("read_reservation_status"), {}, timeout=10.0)

    assert result.ok is False
    assert result.status is ResultStatus.UNAVAILABLE
    assert "尚未实现" in result.warnings[0]


async def _never(selector: str, timeout_seconds: float = 10.0) -> bool:
    return False
