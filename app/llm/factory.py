"""Chooses a concrete LLM client for a call."""

from __future__ import annotations

from app.config.settings import Settings
from app.llm.credential import CredentialProvider, CredentialScope
from app.llm.openai_compat import OpenAICompatClient
from app.llm.profile import LLMProfile
from app.llm.stub import OfflineStubLLM


def build_llm_client(
    settings: Settings,
    credentials: CredentialProvider,
    *,
    request_key: str | None = None,
    profile: LLMProfile | None = None,
):
    """Return ``(client, api_key_or_None, scope_or_None)``.

    Falls back to the deterministic stub when no key resolves or when
    ``LLM_OFFLINE`` is set, so the graph always runs.
    """

    if settings.llm_offline:
        return OfflineStubLLM(), None, None

    if profile is not None and profile.is_local:
        # llama-server needs no credential, but the OpenAI SDK insists on a
        # non-empty string, so a sentinel is supplied here and nowhere else.
        return (
            OpenAICompatClient(
                settings,
                api_key=profile.api_key or "local",
                base_url=profile.base_url,
                model=profile.model,
            ),
            profile.api_key,
            CredentialScope.REQUEST,
        )

    key, scope = credentials.resolve(request_key)
    if not key:
        return OfflineStubLLM(), None, None
    return (
        OpenAICompatClient(
            settings,
            api_key=key,
            base_url=profile.base_url if profile else None,
            model=profile.model if profile else None,
        ),
        key,
        scope,
    )
