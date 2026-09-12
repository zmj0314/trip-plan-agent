"""Credential resolution (framework §11.3, DL-2).

``LLM_CREDENTIAL_MODE`` switches between "server ships a key" and "bring your
own key". The BYOK path threads the key per request and never persists it.

**Invariant: a resolved key is never placed into ``AgentState``.** State is
serialised into the LangGraph checkpoint, so a key in state is a key on disk.
Callers must pass the key through a context object that lives for one call.
"""

from __future__ import annotations

from enum import StrEnum

from app.config.settings import Settings


class CredentialScope(StrEnum):
    SERVER = "server"
    REQUEST = "request"


class CredentialProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def mode(self) -> str:
        return self._settings.llm_credential_mode

    def resolve(self, request_key: str | None = None) -> tuple[str | None, CredentialScope | None]:
        """Return ``(key, scope)``; ``(None, None)`` when no key is available."""

        mode = self._settings.llm_credential_mode
        if mode == "request":
            return (request_key, CredentialScope.REQUEST) if request_key else (None, None)
        if mode == "server":
            key = self._settings.deepseek_api_key
            return (key, CredentialScope.SERVER) if key else (None, None)
        # both
        if request_key:
            return request_key, CredentialScope.REQUEST
        key = self._settings.deepseek_api_key
        return (key, CredentialScope.SERVER) if key else (None, None)

    def describe(self) -> dict[str, object]:
        """Safe to log: never contains key material."""

        has_server_key = bool(self._settings.deepseek_api_key)
        return {
            "mode": self._settings.llm_credential_mode,
            "server_key_configured": has_server_key,
            "model": self._settings.deepseek_model,
            "base_url": self._settings.deepseek_base_url,
        }
