"""The browser adapter: five primitives, one verification rule.

Every mutating call does the same three things, in this order:

1. **Observe** the page before acting (so "did anything change" is answerable);
2. act through the driver;
3. **Observe again and verify** the caller's expectation against the page.

Step 3 is the whole point of this channel. "The tool returned 'clicked'" is not
evidence; the framework's rule is that the page has to have actually moved. An
unmet expectation is returned as ``ResultStatus.ERROR`` with the observed page in
``warnings`` -- never as a success with a note, because a caller cannot be trusted
to read the note before telling the user their booking went through.

Read-only primitives (``navigate``/``read``/``wait``/``screenshot``) still verify
when an expectation is given, but they are allowed to succeed without one.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.browser.contract import (
    BrowserDriver,
    BrowserError,
    Expectation,
    PageObservation,
    verify,
)
from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Degradation,
    DegradationLevel,
    Provenance,
    ResultStatus,
)
from app.capabilities.registry import CapabilityBinding
from app.channels.base import ChannelAdapter, HealthState, HealthStatus
from app.domain.timebase import now_local

#: Which primitive each binding's ``remote_name`` maps to, and whether it changes
#: the page. Kept here rather than in the registry because the risk level in the
#: registry is a *policy* statement while this is a mechanical fact: typing into a
#: field always mutates, whatever the policy says about consent.
PRIMITIVE_BY_REMOTE: dict[str, tuple[str, bool]] = {
    "navigate": ("navigate", True),
    "read": ("read", False),
    "click": ("click", True),
    "type": ("type", True),
    # ``submit`` is a click on a submit control; kept distinct so its consent
    # requirement can differ from an ordinary click.
    "submit": ("click", True),
    "screenshot": ("screenshot", False),
    # A workflow rather than a primitive: it composes the five above against a
    # known site. Read-only, and it verifies the page before returning anything.
    "read_rail_availability": ("rail_availability", False),
    "read_reservation_status": ("reservation_status", False),
}


class BrowserAdapter(ChannelAdapter):
    """Serves ``browser.*`` capabilities against one attached page."""

    id = "browser"
    kind = ChannelKind.BROWSER

    def __init__(self, driver: BrowserDriver, *, observations: int = 400) -> None:
        self._driver = driver
        #: Bounded: a long session on a chatty page must not grow without limit.
        self._log: list[dict[str, Any]] = []
        self._log_limit = max(1, observations)

    # ------------------------------------------------------------- diagnostics
    def supports(self, capability_id: str, remote_name: str) -> bool:
        return remote_name in PRIMITIVE_BY_REMOTE

    async def health(self) -> HealthStatus:
        try:
            alive = await self._driver.healthy()
        except Exception:  # pragma: no cover - a dead driver must not raise here
            alive = False
        return HealthStatus(
            adapter_id=self.id,
            state=HealthState.HEALTHY if alive else HealthState.UNAVAILABLE,
            last_check=now_local(),
        )

    @property
    def observations(self) -> list[dict[str, Any]]:
        return list(self._log)

    async def aclose(self) -> None:
        await self._driver.aclose()

    # ------------------------------------------------------------------ invoke
    async def invoke(
        self,
        binding: CapabilityBinding,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> CapabilityResult:
        primitive, mutating = PRIMITIVE_BY_REMOTE.get(
            binding.remote_name, (binding.remote_name, False)
        )
        expectation = _expectation_from(params)
        selectors = _selectors_from(params)

        before: PageObservation | None = None
        if expectation.any and (mutating or expectation.page_changed):
            try:
                before = await self._driver.observe(selectors=selectors)
            except BrowserError as exc:
                return _error(self.id, f"操作前无法读取页面：{exc}", kind=exc.kind)

        try:
            outcome = await self._act(primitive, binding.remote_name, params)
        except BrowserError as exc:
            return _error(self.id, str(exc), kind=exc.kind, data=outcome_data(before, None))
        except Exception as exc:  # pragma: no cover - driver crash
            return _error(self.id, f"浏览器步骤失败：{type(exc).__name__}: {exc}")

        after = outcome.pop("_observation", None)
        # A workflow knows what its own success looks like (a results table that
        # only exists after a query ran). It may supply that expectation instead
        # of the caller, because the caller cannot know a site's structure.
        expectation = outcome.pop("_expectation", None) or expectation
        if after is None:
            try:
                after = await self._driver.observe(selectors=selectors)
            except BrowserError as exc:
                return _error(self.id, f"操作后无法读取页面：{exc}", kind=exc.kind)

        verdict = verify(expectation, before or PageObservation(), after)
        self._record(
            primitive=primitive,
            expectation=expectation,
            before=before,
            after=after,
            verdict=verdict,
        )

        data = outcome_data(before, after, extra={k: v for k, v in outcome.items() if not k.startswith("_")})
        warnings = [
            *verdict.failures,
            *([] if verdict.verified or verdict.unverified else []),
            *after.unreadable[:3],
        ]

        if verdict.unverified and mutating and expectation.any is False:
            # A mutating step with nothing to check is not "verified ok"; it is
            # "we did not look", and the caller needs to know which one it got.
            warnings.append("该操作没有声明验收条件，仅能确认未报错")
        if not verdict.ok:
            # The reason carries the *verification* failure, not a generic
            # "error": a resolver that falls through to another provider (or
            # degrades to a self-service fallback) must not be able to lose the
            # one fact that matters -- that a side effect did not happen.
            reason = "verification_failed: " + "；".join(verdict.failures)
            return CapabilityResult(
                status=ResultStatus.ERROR,
                data={**data, "verified": False, "failures": list(verdict.failures)},
                provenance=_provenance(self.id),
                degradation=Degradation(level=DegradationLevel.D0, reason=reason),
                warnings=warnings,
            )

        return CapabilityResult(
            status=ResultStatus.OK,
            data={**data, "verified": verdict.verified, "checked": verdict.checked},
            provenance=_provenance(self.id),
            degradation=Degradation(level=DegradationLevel.D0),
            warnings=warnings,
        )

    async def _act(self, primitive: str, remote: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if primitive == "rail_availability":
            return await self._read_rail_availability(params)

        if primitive == "reservation_status":
            # Not implemented: the attraction-reservation flow needs a named site
            # and verified selectors, and shipping a guess would be worse than an
            # explicit absence.
            raise BrowserError("景点预约状态读取尚未实现（缺少验证过的站点选择器）", kind="not_configured")

        if primitive == "navigate":
            url = str(params.get("url") or "").strip()
            if not url:
                raise BrowserError("navigate 需要 url", kind="business")
            observation = await self._driver.navigate(url)
            return {"url": observation.url, "title": observation.title, "_observation": observation}

        if primitive == "read":
            selector = params.get("selector")
            text = await self._driver.read(selector=str(selector) if selector else None)
            return {"text": text, "_observation": await self._driver.observe()}

        if primitive == "click":
            selector = str(params.get("selector") or "").strip()
            if not selector:
                raise BrowserError("click 需要 selector", kind="business")
            await self._driver.click(selector)
            return {}

        if primitive == "type":
            selector = str(params.get("selector") or "").strip()
            if not selector:
                raise BrowserError("type 需要 selector", kind="business")
            await self._driver.type(selector, str(params.get("text") or ""))
            return {}

        if primitive == "screenshot":
            image = await self._driver.screenshot()
            return {
                "bytes": len(image) if image else 0,
                "captured": image is not None,
            }

        if primitive == "wait":
            selector = str(params.get("selector") or "").strip()
            if not selector:
                raise BrowserError("wait 需要 selector", kind="business")
            found = await self._driver.wait_for(
                selector, timeout_seconds=float(params.get("timeout_seconds") or 10.0)
            )
            if not found:
                # A wait that timed out is a *business* outcome, not a crash: the
                # page simply never showed what the plan expected.
                return {"found": False, "_observation": await self._driver.observe()}
            return {"found": True, "_observation": await self._driver.observe()}

        raise BrowserError(f"未知浏览器原语：{remote}", kind="business")

    async def _read_rail_availability(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """One read-only workflow: query 12306 and read the results table."""

        from app.browser.sites import RAIL_SELECTORS, RailFlow, parse_rail_rows

        from_station = str(params.get("fromStation") or params.get("from_station") or "").strip()
        to_station = str(params.get("toStation") or params.get("to_station") or "").strip()
        date = str(params.get("date") or "").strip()
        missing = [
            label
            for label, value in (("fromStation", from_station), ("toStation", to_station), ("date", date))
            if not value
        ]
        if missing:
            raise BrowserError(f"缺少必要参数：{'、'.join(missing)}", kind="business")

        flow = RailFlow(self._driver, selectors=RAIL_SELECTORS)
        text = await flow.run(from_station=from_station, to_station=to_station, date=date)
        rows = parse_rail_rows(text if isinstance(text, str) else "")
        return {
            "trains": rows,
            "rows": len(rows),
            # The selectors are unverified, so the answer says so rather than
            # implying the page structure was confirmed.
            "selectors_verified": RAIL_SELECTORS.verified,
            "_expectation": flow.expectation,
            "_observation": await self._driver.observe(
                selectors=[RAIL_SELECTORS.get("result_table")]
            ),
        }

    def _record(
        self,
        *,
        primitive: str,
        expectation: Expectation,
        before: PageObservation | None,
        after: PageObservation,
        verdict: Any,
    ) -> None:
        self._log.append(
            {
                "at": now_local().isoformat(),
                "primitive": primitive,
                "url": after.url,
                "changed": before is not None and before.digest() != after.digest(),
                "verified": verdict.verified,
                "unverified": verdict.unverified,
                "failures": list(verdict.failures),
                "expectation": expectation.model_dump(exclude_none=True),
            }
        )
        if len(self._log) > self._log_limit:
            del self._log[: len(self._log) - self._log_limit]


def _expectation_from(params: Mapping[str, Any]) -> Expectation:
    raw = params.get("expect")
    if isinstance(raw, Mapping):
        return Expectation.model_validate(dict(raw))
    return Expectation()


def _selectors_from(params: Mapping[str, Any]) -> list[str]:
    selectors = [
        str(value)
        for key, value in params.items()
        if key in {"selector", "expect_selector"} and value
    ]
    raw = params.get("expect")
    if isinstance(raw, Mapping):
        for key in ("selector_present", "selector_absent"):
            if raw.get(key):
                selectors.append(str(raw[key]))
    return selectors


def outcome_data(
    before: PageObservation | None,
    after: PageObservation | None,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The normalised payload: what the page looks like, not what we intended."""

    data: dict[str, Any] = {}
    if after is not None:
        data["url"] = after.url
        data["title"] = after.title
        data["text"] = after.text
        data["present"] = dict(after.present)
        data["foreground"] = after.foreground
        if after.unreadable:
            data["unreadable"] = list(after.unreadable)
    if before is not None and after is not None:
        data["changed"] = before.digest() != after.digest()
    if extra:
        data.update({key: value for key, value in extra.items() if not key.startswith("_")})
    return data


def _provenance(adapter_id: str) -> Provenance:
    return Provenance(
        channel=ChannelKind.BROWSER,
        provider=adapter_id,
        fetched_at=now_local(),
        confidence=1.0,
    )


def _error(adapter_id: str, message: str, *, kind: str = "browser", data: Any = None) -> CapabilityResult:
    """A browser failure is an ERROR -- unless the browser is simply not there.

    Those are two different stories. "The click did nothing" must never be
    softened into a degradation, because a caller would read it as a slightly
    stale answer and tell the user their booking went through. "Chrome was not
    started with a debug port" is the opposite: it means *this provider is
    absent*, so it has to be ``UNAVAILABLE`` for the resolver to move on to the
    next binding -- 12306 availability, for instance, falls back to the MCP
    search. Returning ERROR there would truncate the provider chain.
    """

    unavailable = kind in {"unavailable", "not_configured"}
    return CapabilityResult(
        status=ResultStatus.UNAVAILABLE if unavailable else ResultStatus.ERROR,
        data=data,
        provenance=_provenance(adapter_id),
        degradation=Degradation(level=DegradationLevel.D0, reason=f"{kind}: {message}"),
        warnings=[message],
    )


__all__ = ["PRIMITIVE_BY_REMOTE", "BrowserAdapter", "outcome_data"]
