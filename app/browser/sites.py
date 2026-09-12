"""Site-specific page flows (framework §7, the read-only first step).

The channel has five generic primitives. A *workflow* like "read 12306 ticket
availability" is a sequence of them plus the selectors of one particular site,
and that knowledge belongs here rather than inside the adapter or the node.

## Selectors are declared, not guessed

Every selector in this file is marked with whether it has been checked against
the live site. **None of them have.** They are written from the documented page
structure and will be corrected the first time someone runs this against a real
browser -- which is exactly why the flow verifies the results table before
returning anything: if the selectors are wrong, the user gets "could not verify"
rather than a fabricated answer.

This is the same discipline the data-source work follows: write the code so the
unverified part is *visible* in the result, instead of assuming it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.browser.contract import BrowserDriver, BrowserError, Expectation

#: 12306's ticket query page. Read-only: this flow never submits an order.
RAIL_QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/init"


@dataclass(frozen=True)
class SiteSelectors:
    """Where things are on one page. ``verified`` is the honest bit."""

    verified: bool = False
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        return self.values.get(key, "")


#: The query form and its result table.
RAIL_SELECTORS = SiteSelectors(
    verified=False,
    values={
        # The departure / arrival station inputs and the date field.
        "from_input": "#fromStationText",
        "to_input": "#toStationText",
        "date_input": "#train_date",
        "query_button": "#query_ticket",
        # The results table only exists once a query has run, which is what makes
        # it a usable acceptance marker.
        "result_table": "#queryLeftTable",
        "result_row": "#queryLeftTable tr",
    },
)

#: How long to wait for the results table after pressing query.
RESULT_TIMEOUT_SECONDS = 20.0


class RailFlow:
    """The 12306 availability read, as primitives plus selectors."""

    def __init__(
        self,
        driver: BrowserDriver,
        *,
        selectors: SiteSelectors = RAIL_SELECTORS,
        url: str = RAIL_QUERY_URL,
        timeout_seconds: float = RESULT_TIMEOUT_SECONDS,
    ) -> None:
        self._driver = driver
        self._selectors = selectors
        self._url = url
        self._timeout = timeout_seconds

    @property
    def expectation(self) -> Expectation:
        """What must be true for the result to be worth reading.

        The results table is the marker: asserting on text would pass as soon as
        the page said anything at all, including "no trains matched".
        """

        return Expectation(
            selector_present=self._selectors.get("result_table") or None,
            url_contains="12306",
        )

    async def run(self, *, from_station: str, to_station: str, date: str) -> dict[str, Any]:
        """Fill the query form and read the table. Raises on any failed step.

        The caller (the adapter) turns a raise into an ERROR result and verifies
        the expectation against the page; nothing here decides whether the answer
        is trustworthy.
        """

        selectors = self._selectors
        await self._driver.navigate(self._url)

        for key, value in (
            ("from_input", from_station),
            ("to_input", to_station),
            ("date_input", date),
        ):
            selector = selectors.get(key)
            if not selector:
                raise BrowserError(f"站点选择器缺失：{key}", kind="not_configured")
            await self._driver.type(selector, value)

        button = selectors.get("query_button")
        if not button:
            raise BrowserError("站点选择器缺失：query_button", kind="not_configured")
        await self._driver.click(button)

        table = selectors.get("result_table")
        if table and not await self._driver.wait_for(table, timeout_seconds=self._timeout):
            # A timeout is a *result*: the page may have shown an error, or the
            # selectors may be wrong. Either way there is nothing to read, and
            # saying so is the only honest outcome.
            raise BrowserError(
                f"查询后 {self._timeout:.0f} 秒内未出现结果表（选择器可能已变更）",
                kind="business",
            )

        return await self._driver.read(selector=table) if table else {}


def parse_rail_rows(text: str) -> list[dict[str, Any]]:
    """Best-effort parse of the results table's text.

    Deliberately shallow: without a verified page we do not know the column
    order, and inventing one would produce confidently wrong train times. The raw
    text is what the answer carries; this only pulls out the train codes, which
    are unambiguous because of their shape.
    """

    import re

    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"\b([GDCZTKY]\d{1,4})\b", line)
        if match:
            rows.append({"train_code": match.group(1), "text": line[:200]})
    return rows


__all__ = [
    "RAIL_QUERY_URL",
    "RAIL_SELECTORS",
    "RESULT_TIMEOUT_SECONDS",
    "RailFlow",
    "SiteSelectors",
    "parse_rail_rows",
]
