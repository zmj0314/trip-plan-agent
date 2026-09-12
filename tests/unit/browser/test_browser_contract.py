"""Browser channel safety contract (framework §7, DC-6).

The browser is the only channel where a successful call can mean nothing
happened. These tests pin the rule that makes it usable: **the page has to have
actually moved**, or the action is reported as failed.
"""

from __future__ import annotations

import pytest

from app.browser.adapter import BrowserAdapter
from app.browser.contract import Expectation, PageObservation, verify
from app.capabilities.contract import ResultStatus
from app.capabilities.registry import CapabilityBinding
from tests.unit.browser.fake_driver import (
    FakeBrowserDriver,
    FakePage,
    do_nothing,
    reveal,
    to_background,
)


def _binding(remote: str) -> CapabilityBinding:
    return CapabilityBinding(adapter_id="browser", remote_name=remote)


async def _invoke(adapter: BrowserAdapter, remote: str, params: dict):
    return await adapter.invoke(_binding(remote), params, timeout=10.0)


# ---------------------------------------------------------------------------
# verify() -- pure
# ---------------------------------------------------------------------------


def test_no_expectation_is_reported_as_unverified_not_verified() -> None:
    """An action with nothing to check must not be able to claim verification."""

    verdict = verify(Expectation(), PageObservation(), PageObservation())
    assert verdict.verified is False
    assert verdict.unverified is True
    assert verdict.checked == []
    assert verdict.failures == []


def test_page_changed_detects_a_no_op_click() -> None:
    before = PageObservation(url="https://x/", text="表单")
    same = PageObservation(url="https://x/", text="表单")
    different = PageObservation(url="https://x/", text="表单 已提交")

    assert verify(Expectation(page_changed=True), before, same).ok is False
    assert verify(Expectation(page_changed=True), before, different).ok is True


def test_text_and_selector_expectations_are_both_checked() -> None:
    after = PageObservation(
        url="https://x/done", text="提交成功", present={"#done": True, "#modal": False}
    )
    verdict = verify(
        Expectation(text_contains="提交成功", selector_present="#done", selector_absent="#modal"),
        PageObservation(),
        after,
    )
    assert verdict.ok is True
    assert set(verdict.checked) == {"text_contains", "selector_present", "selector_absent"}


def test_selected_but_unreadable_page_does_not_pass_a_text_check() -> None:
    """Finding the text in what little was legible is not evidence."""

    after = PageObservation(text="提交成功", unreadable=["#frame 无法读取"])
    verdict = verify(Expectation(text_contains="提交成功"), PageObservation(), after)
    assert verdict.ok is False
    assert any("不可靠" in failure for failure in verdict.failures)


def test_every_failure_names_what_was_expected() -> None:
    verdict = verify(
        Expectation(text_contains="成功", selector_present="#ok"),
        PageObservation(),
        PageObservation(text="失败", present={"#ok": False}),
    )
    assert len(verdict.failures) == 2
    assert any("成功" in failure for failure in verdict.failures)
    assert any("#ok" in failure for failure in verdict.failures)


# ---------------------------------------------------------------------------
# BrowserAdapter -- the same rule, through the capability surface
# ---------------------------------------------------------------------------


async def test_a_click_that_changes_nothing_is_a_failure() -> None:
    """The exact lie this channel exists to prevent."""

    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=do_nothing))
    await _invoke(adapter, "navigate", {"url": "https://kyfw.12306.cn/"})

    result = await _invoke(
        adapter,
        "click",
        {"selector": "#submit", "expect": {"page_changed": True}},
    )
    assert result.ok is False
    assert result.status is ResultStatus.ERROR
    assert any("没有任何变化" in warning for warning in result.warnings)
    assert result.data["changed"] is False


async def test_a_click_that_moves_the_page_succeeds() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=reveal))
    await _invoke(adapter, "navigate", {"url": "https://kyfw.12306.cn/"})

    result = await _invoke(
        adapter,
        "click",
        {"selector": "#submit", "expect": {"text_contains": "已提交"}},
    )
    assert result.ok is True
    assert result.data["verified"] is True
    assert result.data["text_contains" if False else "text"].endswith("已提交")


async def test_a_background_tab_is_not_a_success() -> None:
    """A SPA in a background tab renders nothing; every check must fail loudly."""

    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=to_background))
    await _invoke(adapter, "navigate", {"url": "https://x/"})

    result = await _invoke(
        adapter,
        "click",
        {"selector": "#go", "expect": {"text_contains": "结果"}},
    )
    assert result.ok is False
    assert result.data["foreground"] is False
    assert any("后台标签页" in warning for warning in result.warnings)


async def test_a_mutating_step_without_expectations_says_it_did_not_look() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=reveal))
    result = await _invoke(adapter, "click", {"selector": "#submit"})

    assert result.ok is True, "nothing to check is not automatically a failure"
    assert result.data["verified"] is False
    assert any("没有声明验收条件" in warning for warning in result.warnings)


async def test_read_is_allowed_to_succeed_without_an_expectation() -> None:
    driver = FakeBrowserDriver()
    await driver.navigate("https://x/")
    driver.page.text = "G1 次 有票"
    adapter = BrowserAdapter(driver)

    result = await _invoke(adapter, "read", {"selector": "#tickets"})
    assert result.ok is True
    assert "有票" in result.data["text"]
    assert not any("验收条件" in warning for warning in result.warnings)


async def test_a_missing_selector_fails_the_expectation() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=reveal))
    await _invoke(adapter, "navigate", {"url": "https://x/"})

    result = await _invoke(
        adapter, "click", {"selector": "#absent-button", "expect": {"selector_present": "#absent-button"}}
    )
    assert result.ok is False
    assert any("#absent-button" in warning for warning in result.warnings)


async def test_a_navigation_failure_is_reported_with_its_kind() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver(navigate_fails=True))
    result = await _invoke(adapter, "navigate", {"url": "https://unreachable/"})

    assert result.ok is False
    assert "network" in result.degradation.reason


async def test_a_closed_browser_degrades_as_unavailable() -> None:
    driver = FakeBrowserDriver()
    adapter = BrowserAdapter(driver)
    await driver.aclose()

    result = await _invoke(adapter, "read", {})
    assert result.ok is False
    assert result.status is ResultStatus.UNAVAILABLE


async def test_an_unknown_primitive_is_a_business_error() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver())
    result = await adapter.invoke(_binding("download"), {}, timeout=5.0)
    assert result.ok is False
    assert "未知浏览器原语" in result.warnings[0]


async def test_screenshot_never_claims_to_have_verified_anything() -> None:
    adapter = BrowserAdapter(FakeBrowserDriver())
    result = await _invoke(adapter, "screenshot", {})
    assert result.ok is True
    assert result.data["captured"] is True
    assert result.data["verified"] is False


async def test_every_step_is_recorded_with_its_verdict() -> None:
    """The ledger of what was actually done, for the audit trail and the UI."""

    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=do_nothing))
    await _invoke(adapter, "navigate", {"url": "https://x/"})
    await _invoke(adapter, "click", {"selector": "#buy", "expect": {"page_changed": True}})

    log = adapter.observations
    assert [entry["primitive"] for entry in log] == ["navigate", "click"]
    assert log[-1]["verified"] is False
    assert log[-1]["failures"]


async def test_health_reflects_the_driver() -> None:
    alive = await BrowserAdapter(FakeBrowserDriver()).health()
    dead = await BrowserAdapter(FakeBrowserDriver(healthy=False)).health()
    assert alive.state.value == "healthy"
    assert dead.state.value == "unavailable"


async def test_the_driver_sees_an_observation_before_and_after_a_mutation() -> None:
    """``page_changed`` is only answerable because both observations are taken."""

    driver = FakeBrowserDriver(click_effect=reveal)
    adapter = BrowserAdapter(driver)
    await _invoke(adapter, "click", {"selector": "#go", "expect": {"page_changed": True}})

    primitives = [name for name, _ in driver.calls if name == "observe"]
    assert len(primitives) >= 2, "before and after must both be observed"


async def test_wait_reports_a_timeout_as_a_result_not_a_crash() -> None:
    driver = FakeBrowserDriver()
    driver.wait_for = lambda selector, timeout_seconds=10.0: _false()  # type: ignore[assignment]
    adapter = BrowserAdapter(driver)

    result = await _invoke(adapter, "wait", {"selector": "#tickets", "timeout_seconds": 1})
    assert result.ok is True
    assert result.data["found"] is False


async def _false() -> bool:
    return False


def test_fake_page_presence_rule_is_explicit() -> None:
    """Guards the fake itself: a test could otherwise pass for the wrong reason."""

    page = FakePage()
    assert page.observe(["#buy"]).present == {"#buy": True}
    assert page.observe(["#absent-buy"]).present == {"#absent-buy": False}
