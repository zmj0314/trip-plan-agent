"""LLM provider profile (DM-3).

Separates *which endpoint* from *which credential*. A profile is per-request
and never persisted: like a key, an endpoint choice is request-scoped data
(§11.3.4).

The reason a local model needs no new client: ``llama-server`` implements the
OpenAI protocol (``/v1/chat/completions``), so "local" is a base_url, not a
code path.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.config.settings import Settings


class LLMProvider(StrEnum):
    DEEPSEEK = "deepseek"
    LOCAL = "local"


class LLMProfile(BaseModel):
    provider: LLMProvider = LLMProvider.DEEPSEEK
    base_url: str = ""
    model: str = ""
    #: Held in memory for the duration of one request. Never logged, never
    #: persisted, never placed in graph state.
    api_key: str | None = Field(default=None, repr=False)

    @property
    def is_local(self) -> bool:
        return self.provider is LLMProvider.LOCAL


class ProfileError(ValueError):
    pass


def normalize_base_url(raw: str) -> str:
    """Accept what people actually paste and make it usable.

    Handles: trailing slashes, a pasted ``/chat/completions`` suffix, and a
    bare host:port with no ``/v1``.
    """

    text = (raw or "").strip().rstrip("/")
    if not text:
        raise ProfileError("base_url 不能为空")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileError("base_url 必须是 http 或 https")
    if not parsed.netloc:
        raise ProfileError("base_url 缺少主机名")
    for suffix in ("/chat/completions", "/completions"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if not urlparse(text).path:
        text = f"{text}/v1"
    return text.rstrip("/")


def resolve_profile(
    settings: Settings,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> LLMProfile:
    chosen = LLMProvider((provider or settings.llm_provider).strip().lower())
    if chosen is LLMProvider.LOCAL:
        return LLMProfile(
            provider=chosen,
            base_url=normalize_base_url(base_url or settings.local_llm_base_url),
            model=(model or settings.local_llm_model).strip() or settings.local_llm_model,
            api_key=(api_key or None),
        )
    return LLMProfile(
        provider=chosen,
        base_url=normalize_base_url(base_url or settings.deepseek_base_url),
        model=(model or settings.deepseek_model).strip() or settings.deepseek_model,
        api_key=(api_key or None),
    )
