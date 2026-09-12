"""Provider chain, degradation ladder and arbitration (framework §3.3, §4.1.2).

This is the only door between the orchestration layer and the outside world.
Two guarantees live here:

* **Nothing runs before it is allowed.** When an arbiter is configured, every
  call is arbitrated first -- that is what makes "zero non-read-only calls
  before consent" a property of the system rather than a convention.
* **Nothing fails silently.** A dead provider walks down a declared ladder
  (fresh cache → live source → backup source → stale cache → skeleton →
  deep-link) and the reason travels back with the result
  (framework §6.10.6).

The ladder is data, not control flow: :class:`CapabilitySpec.degradation_ladder`
decides how far a capability is allowed to fall, so a provider can be removed
without touching the graph.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Degradation,
    DegradationLevel,
    Provenance,
    ResultStatus,
)
from app.capabilities.registry import CapabilityRegistry
from app.channels.base import ChannelAdapter
from app.channels.cache import CacheStore
from app.channels.quota import QuotaDecision, QuotaLedger
from app.channels.resilience import backoff_seconds, classify, should_retry, with_timeout
from app.domain.ids import new_id
from app.domain.timebase import now_local
from app.policy.risk import Arbiter

INTERNAL_CONSENT_REF = "internal:read-only"

#: A provider enforces its own budget; this is the backstop that stops a wedged
#: one from holding a turn open forever.
DEFAULT_TIMEOUT_GRACE = 2.0


class CallContext(BaseModel):
    """What the caller can prove about consent, per call."""

    session_id: str | None = None
    plan_consented: bool = False
    consent_ref: str | None = None
    has_action_consent: bool = False
    confirm_token_ok: bool = False
    allow_network: bool = True


class CapabilityResolver:
    def __init__(
        self,
        registry: CapabilityRegistry,
        adapters: Mapping[str, ChannelAdapter],
        cache: CacheStore | None = None,
        quota: QuotaLedger | None = None,
        arbiter: Arbiter | None = None,
        *,
        retry_max_attempts: int = 2,
        timeout_grace: float = DEFAULT_TIMEOUT_GRACE,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)
        self._cache = cache
        self._quota = quota
        self._arbiter = arbiter
        self._retry_max_attempts = max(0, int(retry_max_attempts))
        self._timeout_grace = max(0.0, float(timeout_grace))
        self._sleep = sleep

    @property
    def adapters(self) -> dict[str, ChannelAdapter]:
        return dict(self._adapters)

    # ------------------------------------------------------------------- call
    async def call(
        self,
        capability_id: str,
        params: dict[str, Any],
        *,
        call_context: CallContext | None = None,
    ) -> CapabilityResult:
        params = dict(params or {})
        spec = self._registry.spec(capability_id)  # unknown id -> CapabilityNotFound

        denial = self._arbitrate(capability_id, spec.risk, call_context)
        if denial is not None:
            return denial
        context = self._effective_context(spec.risk, call_context)

        key = self._cache_key(capability_id, params, spec.cache_ttl_seconds)
        if key is not None and self._cache is not None:
            entry = self._cache.get_fresh(key)
            if entry is not None:
                return _from_cache(capability_id, entry)

        warnings: list[str] = []
        for binding in self._registry.bindings(capability_id):
            adapter = self._adapters.get(binding.adapter_id)
            if adapter is None:
                warnings.append(f"{binding.adapter_id}: adapter not configured")
                continue
            if not context.allow_network and adapter.id != "local":
                warnings.append(f"{binding.adapter_id}: network disabled for this call")
                continue
            if not adapter.supports(capability_id, binding.remote_name):
                warnings.append(f"{binding.adapter_id}: does not serve {binding.remote_name}")
                continue

            result = await self._attempt(capability_id, spec, binding, adapter, params, warnings, key)
            if result is not None:
                return result

        return self._ladder(capability_id, spec, params, key, warnings)

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            closer = getattr(adapter, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # pragma: no cover - shutdown must not raise
                    pass

    # -------------------------------------------------------------- arbitration
    def _effective_context(self, risk: Any, call_context: CallContext | None) -> CallContext:
        """What consent a call carries when the caller supplied no context.

        An internal call that states nothing is treated as read-only: L0 work is
        by definition free of side effects, so it may proceed, and it is marked
        with an internal consent reference so the audit trail says what it
        relied on. Anything with side effects is refused instead of assumed --
        the alternative would be a silent path around the gate.
        """

        if call_context is not None:
            return call_context
        if risk.requires_consent:
            return CallContext(plan_consented=False, consent_ref=None)
        return CallContext(plan_consented=True, consent_ref=INTERNAL_CONSENT_REF)

    def _arbitrate(self, capability_id: str, risk: Any, call_context: CallContext | None) -> CapabilityResult | None:
        if self._arbiter is None:
            return None
        context = self._effective_context(risk, call_context)
        award = self._arbiter.decide(
            capability_id=capability_id,
            plan_consented=context.plan_consented,
            consent_ref=context.consent_ref,
            has_action_consent=context.has_action_consent,
            confirm_token_ok=context.confirm_token_ok,
        )
        if award.allowed:
            return None
        return CapabilityResult(
            status=ResultStatus.ERROR,
            degradation=Degradation(level=DegradationLevel.D0, reason=award.reason),
            warnings=[f"capability call denied: {capability_id} ({award.reason})"],
        )

    # ----------------------------------------------------------------- attempt
    async def _attempt(
        self,
        capability_id: str,
        spec: Any,
        binding: Any,
        adapter: ChannelAdapter,
        params: dict[str, Any],
        warnings: list[str],
        key: str | None,
    ) -> CapabilityResult | None:
        quota_idem = new_id("q")
        quota_warnings: list[str] = []
        if self._quota is not None and spec.quota_pool:
            decision = self._quota.reserve(spec.quota_pool, spec.units_per_call, idem=quota_idem)
            if decision is QuotaDecision.EXHAUSTED:
                warnings.append(f"{spec.quota_pool}: quota exhausted")
                return None
            if decision in {QuotaDecision.WARN, QuotaDecision.DEGRADE}:
                quota_warnings.append(f"{spec.quota_pool}: {decision.value}")

        attempts = 1 + self._retry_max_attempts
        last: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = await with_timeout(
                    adapter.invoke(binding, params, timeout=spec.timeout_seconds),
                    spec.timeout_seconds + self._timeout_grace,
                )
            except BaseException as exc:  # noqa: BLE001 - classified below
                last = exc
                if not self._should_retry(exc, spec):
                    break
                await self._sleep(backoff_seconds(attempt))
                continue

            if result.status is ResultStatus.ERROR:
                if self._quota is not None and spec.quota_pool:
                    self._quota.rollback(quota_idem)
                warnings.extend(
                    [
                        f"{binding.adapter_id}: {result.degradation.reason or 'error'}",
                        # Provider-specific evidence (why the click did not take
                        # effect, which selector was missing) is the part a person
                        # can act on. Dropping it leaves a degradation notice that
                        # says "something failed" and nothing more.
                        *[f"{binding.adapter_id}: {note}" for note in result.warnings[:3]],
                    ]
                )
                return None
            if result.status is ResultStatus.UNAVAILABLE:
                # "This provider is not here" is not "here is your answer".
                # Returning it would stop the chain at the first missing source,
                # so a capability with the browser bound first and 12306 second
                # would report nothing at all whenever Chrome was not running --
                # and every unit test of each adapter would still pass.
                if self._quota is not None and spec.quota_pool:
                    self._quota.rollback(quota_idem)
                warnings.append(
                    f"{binding.adapter_id}: {result.degradation.reason or 'unavailable'}"
                )
                return None
            if self._quota is not None and spec.quota_pool:
                self._quota.commit(quota_idem)
            result.warnings = [*quota_warnings, *result.warnings]
            warnings.extend(quota_warnings)
            self._store(key, capability_id, spec, result)
            return result

        if self._quota is not None and spec.quota_pool:
            self._quota.rollback(quota_idem)
        warnings.append(_explain(binding.adapter_id, last))
        return None

    def _should_retry(self, exc: BaseException, spec: Any) -> bool:
        return should_retry(classify(exc), risk=spec.risk)

    # ------------------------------------------------------------------ ladder
    def _ladder(
        self,
        capability_id: str,
        spec: Any,
        params: dict[str, Any],
        key: str | None,
        warnings: list[str],
    ) -> CapabilityResult:
        """What to return when no provider could answer.

        The ladder exists so a *read* can still be useful without live data: a
        skeleton, a cache, a deep link. It must not be applied to a capability
        that changes something, though -- ``DEGRADED`` counts as ``ok``, so
        degrading a failed booking would report success for an action that never
        happened. Side effects fail as ``ERROR``, because "go do it yourself" is
        not a degradation of "we did it for you".
        """

        reason = "ALL_PROVIDERS_FAILED"
        side_effecting = spec.risk.requires_consent
        for level in spec.degradation_ladder:
            if side_effecting:
                break
            if level is DegradationLevel.D1 and key is not None and self._cache is not None:
                entry = self._cache.get(key)  # stale is acceptable here
                if entry is not None:
                    result = _from_cache(capability_id, entry, stale=True)
                    result.warnings = [*warnings, *result.warnings]
                    return result
            if level is DegradationLevel.D2:
                return CapabilityResult(
                    status=ResultStatus.DEGRADED,
                    data=None,
                    degradation=Degradation(level=DegradationLevel.D2, reason=reason),
                    warnings=[*warnings, "未经实时校验，请以官方渠道为准"],
                )
            if level is DegradationLevel.D3:
                return CapabilityResult(
                    status=ResultStatus.DEGRADED,
                    data={"fallback": "self_service", "capability": capability_id, "params": params},
                    degradation=Degradation(level=DegradationLevel.D3, reason=reason),
                    warnings=[*warnings, "已降级为自助跳转 + 清单"],
                )

        status = ResultStatus.ERROR if side_effecting else ResultStatus.UNAVAILABLE
        return CapabilityResult(
            status=status,
            degradation=Degradation(level=DegradationLevel.D3, reason=reason),
            warnings=warnings,
        )

    # ------------------------------------------------------------------- cache
    def _cache_key(self, capability_id: str, params: dict[str, Any], ttl: int | None) -> str | None:
        if self._cache is None or not ttl:
            return None
        return self._cache.make_key(capability_id, params)

    def _store(self, key: str | None, capability_id: str, spec: Any, result: CapabilityResult) -> None:
        if key is None or self._cache is None or not spec.cache_ttl_seconds:
            return
        if not result.ok:
            return
        self._cache.put(
            key,
            result,
            ttl_seconds=int(spec.cache_ttl_seconds),
            capability_id=capability_id,
        )


def _from_cache(capability_id: str, entry: Any, *, stale: bool = False) -> CapabilityResult:
    provenance = entry.provenance
    if provenance is not None:
        provenance = provenance.model_copy(
            update={"cache_hit": True, "ttl_left_seconds": entry.ttl_left()}
        )
    else:
        provenance = Provenance(
            channel=ChannelKind.HTTP,
            provider="cache",
            fetched_at=entry.fetched_at,
            cache_hit=True,
            ttl_left_seconds=entry.ttl_left(),
        )
    warnings = [f"来自缓存（{entry.fetched_at.isoformat()}）"]
    if stale:
        warnings.append("缓存已过期，数据可能已经变化")
    return CapabilityResult(
        status=ResultStatus.DEGRADED,
        data=entry.payload,
        provenance=provenance,
        degradation=Degradation(level=DegradationLevel.D1, reason="CACHE_HIT"),
        warnings=warnings,
    )


def _explain(adapter_id: str, exc: BaseException | None) -> str:
    if exc is None:
        return f"{adapter_id}: unusable response"
    return f"{adapter_id}: {classify(exc).value} ({type(exc).__name__})"


__all__ = ["CallContext", "CapabilityResolver", "DEFAULT_TIMEOUT_GRACE", "INTERNAL_CONSENT_REF"]
