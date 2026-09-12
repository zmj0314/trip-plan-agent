"""LLM provider diagnostics (DM-3).

The point of ``/llm/probe`` is to answer "is this endpoint actually usable?"
before the user starts planning. It makes one minimal real call, so a pass here
means the whole path -- headers, profile, client, network -- works.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.api.context import client_name
from app.llm.factory import build_llm_client
from app.llm.profile import LLMProvider, ProfileError, resolve_profile
from app.llm.stub import OfflineStubLLM
from app.llm.telemetry import TELEMETRY

router = APIRouter()


class ProbeRequest(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class ProbeOutput(BaseModel):
    ok: bool = True
    note: str = ""


@router.post("/llm/probe")
async def probe(
    body: ProbeRequest,
    request: Request,
    x_access_token: str | None = Header(default=None),
) -> dict[str, Any]:
    ctx = request.app.state.ctx
    expected = ctx.settings.access_token
    if expected and x_access_token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        profile = resolve_profile(
            ctx.settings,
            provider=body.provider,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key,
        )
    except (ProfileError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    # Probe the client the *next turn* will actually use, rather than a
    # throwaway one: "test connection" is only meaningful if a pass here means
    # the conversation will work, and it keeps active_client / key_source in
    # agreement instead of reporting two different stories.
    ctx.apply_request_credentials(profile.api_key, profile=profile)
    client = ctx.runner.deps.llm
    # The stub always "succeeds" at being a stub. Reporting that as a healthy
    # endpoint would be the exact silent-success the framework forbids.
    using_stub = isinstance(client, OfflineStubLLM)
    started = time.perf_counter()
    response = await client.structured(
        node="probe",
        schema=ProbeOutput,
        prompt='只输出一个 JSON 对象：{"ok": true, "note": "pong"}',
        max_repair=0,
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    return {
        "ok": bool(response.ok) and not using_stub,
        "note": "未配置密钥，当前会回退到离线规则桩" if using_stub else None,
        "provider": profile.provider.value,
        "client": client_name(client),
        "base_url": profile.base_url,
        "model": response.usage.model or profile.model,
        "latency_ms": latency_ms,
        "available_models": await _list_models(profile.base_url, profile.api_key),
        "raw": (response.raw_text or "")[:300],
        "error": response.error,
        "local": profile.provider is LLMProvider.LOCAL,
    }


async def _list_models(base_url: str, api_key: str | None) -> list[str]:
    """Best-effort ``GET /models``; never let it fail the probe."""

    try:
        import httpx

        headers = {"Authorization": f"Bearer {api_key or 'local'}"}
        async with httpx.AsyncClient(timeout=6.0) as http:
            resp = await http.get(f"{base_url.rstrip('/')}/models", headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        return [str(item.get("id")) for item in payload.get("data", [])][:20]
    except Exception:
        return []
