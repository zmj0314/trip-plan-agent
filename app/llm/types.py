"""LLM call result types."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LLMUsage(BaseModel):
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class LLMResponse(BaseModel):
    """A structured-output call result."""

    parsed: BaseModel | None = None
    raw_text: str = ""
    usage: LLMUsage = Field(default_factory=LLMUsage)
    repaired: bool = False
    ok: bool = True
    error: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class LLMCallContext(BaseModel):
    """Per-call context. Deliberately *not* part of ``AgentState``."""

    model_config = {"arbitrary_types_allowed": True}

    session_id: str | None = None
    request_key: str | None = None
    allow_network: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)
