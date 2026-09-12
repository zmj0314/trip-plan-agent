"""One client for every OpenAI-compatible endpoint.

Covers the hosted DeepSeek API and a local ``llama-server`` (llama.cpp) with the
same code, because the difference between them is a base_url and a model name --
not a protocol (DM-3).

The key is held on the instance, never passed in by callers: requiring every
node to thread a credential is how a key ends up in graph state (DL-4) and how
calls silently no-op when someone forgets.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config.settings import Settings
from app.llm.telemetry import TELEMETRY
from app.llm.types import LLMResponse, LLMUsage

REPAIR_SUFFIX = (
    "\n\n[修正请求] 你上一次的输出不是合法 JSON 或不符合 schema。"
    "请只输出一个合法 JSON 对象，不要任何额外文字。"
)


class OpenAICompatClient:
    def __init__(
        self,
        settings: Settings,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        self._base_url = base_url or settings.deepseek_base_url
        self._model = model or settings.deepseek_model
        self._timeout = timeout or settings.local_llm_request_timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    async def structured(
        self,
        *,
        node: str,
        schema: type[BaseModel],
        prompt: str,
        user_input: str = "",
        context: Any = None,
        api_key: str | None = None,
        allow_network: bool = True,
        max_repair: int = 1,
        on_delta: Any = None,
    ) -> LLMResponse:
        """One structured call.

        ``on_delta`` switches the request to ``stream=True`` and forwards each
        content chunk to that async callback. Streaming changes *when* text
        becomes visible, not *what* is trusted: the final message is still parsed
        and validated against ``schema``, and the streamed text is the raw JSON,
        which a client is expected to discard once the node's own rendered text
        arrives (see ``app/llm/streaming.py``).
        """

        key = api_key or self._api_key
        if key is None:
            TELEMETRY.record(node=node, client=self.model, ok=False, error="no api key resolved")
            return LLMResponse(ok=False, error="no api key resolved", parsed=None)
        if not allow_network:
            TELEMETRY.record(node=node, client=self.model, ok=False, error="network disabled for this call")
            return LLMResponse(ok=False, error="network disabled for this call")

        from openai import AsyncOpenAI  # imported lazily so offline runs need no SDK

        client = AsyncOpenAI(api_key=key, base_url=self._base_url, timeout=self._timeout)
        text = prompt
        repaired = False
        last_error = ""
        usage = LLMUsage(model=self._model)
        # Some local servers reject `response_format`; discover that once and
        # stop asking, rather than failing every call from then on.
        use_json_mode = True
        # Streaming is used for the *first* attempt only. A repair re-asks the
        # model, and streaming the second attempt would interleave two different
        # answers on one node; the client is told to replace what it saw instead.
        stream_this_call = on_delta is not None

        for attempt in range(max_repair + 1):
            try:
                params: dict[str, Any] = {
                    "model": self._model,
                    "messages": [{"role": "user", "content": text}],
                    "temperature": 0.0,
                }
                if use_json_mode:
                    params["response_format"] = {"type": "json_object"}
                if stream_this_call:
                    params["stream"] = True
                    # Ask for usage on the final chunk; servers that do not know
                    # the option ignore it, and running totals stay at zero.
                    params["stream_options"] = {"include_usage": True}
                if stream_this_call:
                    content, usage, failure = await self._consume_stream(client, params, node, on_delta)
                    if failure is not None:
                        if use_json_mode and "response_format" in failure:
                            use_json_mode = False
                            continue
                        TELEMETRY.record(node=node, client=self.model, ok=False, error=failure)
                        return LLMResponse(ok=False, error=failure, parsed=None, usage=usage)
                else:
                    completion = await client.chat.completions.create(**params)
                    content = completion.choices[0].message.content or ""
                    if completion.usage is not None:
                        usage = LLMUsage(
                            model=self._model,
                            input_tokens=completion.usage.prompt_tokens or 0,
                            output_tokens=completion.usage.completion_tokens or 0,
                            cached_input_tokens=getattr(completion.usage, "prompt_cache_hit_tokens", 0) or 0,
                        )
            except Exception as exc:
                message = str(exc)
                if use_json_mode and "response_format" in message:
                    use_json_mode = False
                    continue
                TELEMETRY.record(node=node, client=self.model, ok=False, error=message)
                return LLMResponse(ok=False, error=f"{type(exc).__name__}: {exc}", parsed=None, usage=usage)

            try:
                parsed = schema.model_validate(_loads_lenient(content))
                TELEMETRY.record(
                    node=node,
                    client=self.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
                return LLMResponse(
                    parsed=parsed,
                    raw_text=content,
                    usage=usage,
                    repaired=repaired,
                )
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                # Keep the raw response with the error: a schema failure with no
                # example of what the model actually said is unreproducible, and
                # "invalid response" was exactly the unhelpful message the intake
                # node used to surface.
                last_error = f"{type(exc).__name__}: {exc} | raw={content[:300]!r}"
                repaired = True
                text = prompt + REPAIR_SUFFIX
                # The next attempt is not streamed, so the client must drop what
                # it saw for this node.
                stream_this_call = False
                if on_delta is not None:
                    await on_delta(None)

        TELEMETRY.record(node=node, client=self.model, ok=False, error=last_error)
        return LLMResponse(ok=False, error=last_error, raw_text="", usage=usage, repaired=repaired)

    async def _consume_stream(
        self,
        client: Any,
        params: dict[str, Any],
        node: str,
        on_delta: Any,
    ) -> tuple[str, LLMUsage, str | None]:
        """Read a streamed completion to the end, forwarding chunks.

        Returns ``(content, usage, failure)``. A failure is returned rather than
        raised so the caller's ``response_format`` discovery and repair logic stay
        in one place.
        """

        chunks: list[str] = []
        usage = LLMUsage(model=self._model)
        try:
            stream = await client.chat.completions.create(**params)
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage = LLMUsage(
                        model=self._model,
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                        cached_input_tokens=getattr(chunk.usage, "prompt_cache_hit_tokens", 0) or 0,
                    )
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                piece = getattr(choices[0].delta, "content", None)
                if not piece:
                    continue
                chunks.append(piece)
                if on_delta is not None:
                    await on_delta(piece)
        except Exception as exc:
            return "".join(chunks), usage, f"{type(exc).__name__}: {exc}"
        return "".join(chunks), usage, None


def _loads_lenient(content: str) -> Any:
    """Local models like to wrap JSON in prose or a fenced block."""

    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


#: Backwards-compatible alias: the DeepSeek API is just one profile.
DeepSeekClient = OpenAICompatClient
