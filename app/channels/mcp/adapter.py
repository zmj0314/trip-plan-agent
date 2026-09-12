"""MCP stdio client (framework §6, §6.10).

The handoff pins nearly every source behind MCP, and §6.10.1 forbids the
orchestration layer from knowing any tool name. This adapter is where those
names live, so swapping ``amap_geocode`` for another provider is a binding
change rather than a graph change.

Two rules shape the implementation:

* **Failure is never fatal.** A missing ``npx``, a server that never answers,
  or a tool that errors all resolve to an ``UNAVAILABLE`` result. The trip is
  still planned from whatever else is alive (framework §6.10.2).
* **Health checks are protocol-only** (DG-6). ``health()`` may ``initialize``
  and list tools; it must never issue a billable business call just to answer
  "are you there?".
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Cost,
    Degradation,
    DegradationLevel,
    Provenance,
    ResultStatus,
)
from app.capabilities.registry import CapabilityBinding
from app.channels.base import ChannelAdapter, ChannelError, HealthState, HealthStatus, unavailable
from app.domain.timebase import now_local

PROTOCOL_VERSION = "2024-11-05"


class McpError(ChannelError):
    """The server answered with a JSON-RPC error object."""


class McpStdioAdapter(ChannelAdapter):
    """One MCP server, launched on demand and spoken to over stdin/stdout."""

    id = "mcp"
    kind = ChannelKind.MCP

    def __init__(
        self,
        *,
        adapter_id: str,
        command: str,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        connect_timeout: float = 5.0,
        request_timeout: float = 30.0,
        launcher: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.id = adapter_id
        self.command = command
        self.args = list(args)
        self.env = dict(env or {})
        self.connect_timeout = float(connect_timeout)
        self.request_timeout = float(request_timeout)
        self._launcher = launcher

        self._proc: Any = None
        self._tools: set[str] = set()
        self._state = HealthState.UNAVAILABLE
        self._initialized = False
        self._detail = "not started"
        self._fail_count = 0
        self._last_check = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle
    @property
    def tools(self) -> frozenset[str]:
        return frozenset(self._tools)

    def supports(self, capability_id: str, remote_name: str) -> bool:
        # Before the first handshake the tool list is unknown, so stay optimistic
        # and let the call fail -- and degrade -- if the tool is not there.
        if not self._tools:
            return True
        return remote_name in self._tools

    async def _spawn(self) -> Any:
        if self._launcher is not None:
            return await self._launcher()
        env = {**os.environ, **self.env}
        return await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def start(self) -> bool:
        """Launch and handshake. Returns whether the server is usable."""

        async with self._lock:
            if not self._dead() and self._state is not HealthState.UNAVAILABLE:
                return True
            if self._proc is not None:
                # A corpse still occupies the slot; clear it before respawning,
                # otherwise the pipes of the dead server leak into the new one.
                await self._shutdown()
            try:
                self._proc = await asyncio.wait_for(self._spawn(), timeout=self.connect_timeout)
                stderr = getattr(self._proc, "stderr", None)
                if stderr is not None:
                    self._stderr_task = asyncio.create_task(_drain(stderr))
                await self._request(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "travel-plan-agent", "version": "0.0.1"},
                    },
                    timeout=self.connect_timeout,
                )
                self._initialized = True
                self._notify("notifications/initialized", {})
                listing = await self._request("tools/list", {}, timeout=self.connect_timeout)
                self._tools = _tool_names(listing)
                self._state = HealthState.HEALTHY
                self._detail = f"{len(self._tools)} tools"
            except Exception as exc:
                await self._shutdown()
                self._fail_count += 1
                self._state = HealthState.UNAVAILABLE
                # Diagnostic only: this string never carries a credential.
                self._detail = f"{type(exc).__name__}: {exc}"[:200]
                self._last_check = now_local()
                return False
            self._last_check = now_local()
            return True

    async def _shutdown(self) -> None:
        proc, self._proc = self._proc, None
        self._initialized = False
        self._tools = set()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if proc is None:
            return
        stdin = getattr(proc, "stdin", None)
        try:
            if stdin is not None and not stdin.is_closing():
                stdin.close()
        except Exception:  # pragma: no cover - already dead
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            kill = getattr(proc, "kill", None)
            if kill is not None:
                try:
                    kill()
                except Exception:  # pragma: no cover - already dead
                    pass

    async def aclose(self) -> None:
        await self._shutdown()
        self._state = HealthState.UNAVAILABLE

    async def health(self) -> HealthStatus:
        """Protocol-level liveness only; never a business call (DG-6)."""

        if self._dead():
            await self.start()
        return HealthStatus(
            adapter_id=self.id,
            state=self._state,
            last_check=self._last_check or now_local(),
            fail_count=self._fail_count,
            detail=self._detail,
        )

    def _dead(self) -> bool:
        """A process that has exited is still an object; check its return code.

        Treating ``_proc is not None`` as "alive" is what let a dead server keep
        its HEALTHY badge while every call failed against it.
        """

        proc = self._proc
        if proc is None:
            return True
        return getattr(proc, "returncode", None) is not None

    async def _note_disconnect(self, reason: str) -> None:
        """Drop the corpse so the next attempt can start a fresh server."""

        await self._shutdown()
        self._fail_count += 1
        self._state = HealthState.UNAVAILABLE
        self._detail = reason[:200]
        self._last_check = now_local()

    # --------------------------------------------------------------- JSON-RPC
    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or getattr(proc, "stdin", None) is None:
            return
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": dict(params)}, ensure_ascii=False)
        proc.stdin.write((payload + "\n").encode("utf-8"))

    async def _request(self, method: str, params: Mapping[str, Any], *, timeout: float) -> Any:
        proc = self._proc
        if proc is None or getattr(proc, "stdin", None) is None:
            raise McpError("server is not running", kind="network")
        if method == "tools/call" and not self._initialized:
            raise McpError("server is not initialized", kind="network")

        self._next_id += 1
        message_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": dict(params)}
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        return await asyncio.wait_for(self._await_response(message_id), timeout=timeout)

    async def _await_response(self, message_id: int) -> Any:
        proc = self._proc
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise McpError("server closed stdout", kind="network")
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                # Servers may log to stdout; skip noise instead of corrupting
                # the stream (framework §10: tolerate partial input).
                continue
            if not isinstance(message, dict) or message.get("id") != message_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                text = error.get("message") if isinstance(error, dict) else str(error)
                code = error.get("code") if isinstance(error, dict) else None
                if code in {-32001, 401, 403}:
                    # Variflight returns 403 once the free allowance is gone.
                    raise McpError(f"{self.id}: {text}", kind="quota", retryable=False)
                raise McpError(f"{self.id}: {text}", kind="business", retryable=False)
            return message.get("result")

    # ----------------------------------------------------------------- invoke
    async def invoke(
        self,
        binding: CapabilityBinding,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> CapabilityResult:
        if self._dead() or self._state is HealthState.UNAVAILABLE:
            if not await self.start():
                return unavailable(self.id, f"server unavailable ({self._detail})")

        arguments = _apply_param_map(binding, params)
        call = {"name": binding.remote_name, "arguments": arguments}
        limit = timeout or self.request_timeout

        try:
            result = await self._request("tools/call", call, timeout=limit)
        except McpError as exc:
            # Only a transport failure means the server died. A tool-level error
            # is a perfectly healthy server answering "no", and reconnecting for
            # it would burn a cold-start for nothing.
            if getattr(exc, "kind", "business") != "network":
                raise
            await self._note_disconnect(f"{type(exc).__name__}: {exc}")
            if not await self.start():
                raise
            # One reconnect, one retry. A second failure is reported, not looped.
            result = await self._request("tools/call", call, timeout=limit)
        except Exception as exc:
            self._fail_count += 1
            raise ChannelError(f"{self.id}: {type(exc).__name__}", kind="network") from exc

        if isinstance(result, dict) and result.get("isError"):
            raise McpError(
                f"{self.id}: tool {binding.remote_name} returned an error",
                kind="business",
                retryable=False,
            )

        return CapabilityResult(
            status=ResultStatus.OK,
            data=_apply_result_map(binding, _structured(result)),
            provenance=Provenance(
                channel=ChannelKind.MCP,
                provider=self.id,
                fetched_at=now_local(),
                confidence=1.0,
            ),
            degradation=Degradation(level=DegradationLevel.D0),
            cost=Cost(units=0),
        )


async def _drain(stream: Any) -> None:
    """Keep stderr flowing so a chatty server cannot deadlock on a full pipe."""

    try:
        while True:
            chunk = await stream.readline()
            if not chunk:
                return
    except Exception:  # pragma: no cover - best effort, including cancellation
        return


def _tool_names(listing: Any) -> set[str]:
    if not isinstance(listing, dict):
        return set()
    tools = listing.get("tools")
    if not isinstance(tools, list):
        return set()
    return {str(tool.get("name")) for tool in tools if isinstance(tool, dict) and tool.get("name")}


def _structured(result: Any) -> Any:
    """Prefer MCP's structured payload, falling back to parsed text content."""

    if isinstance(result, dict):
        if "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"
            ]
            if len(texts) == 1:
                try:
                    return json.loads(texts[0])
                except (TypeError, ValueError):
                    return {"text": texts[0]}
            if texts:
                return {"texts": texts}
    return result


def _apply_param_map(binding: CapabilityBinding, params: Mapping[str, Any]) -> dict[str, Any]:
    if not binding.param_map:
        return dict(params or {})
    return {target: params.get(source) for source, target in binding.param_map.items() if source in params}


def _apply_result_map(binding: CapabilityBinding, data: Any) -> Any:
    if not binding.result_map or not isinstance(data, Mapping):
        return data
    return {target: data.get(source) for source, target in binding.result_map.items()}


def npx_command() -> str:
    """``npx`` on Windows is a ``.cmd`` shim; ``exec`` needs the resolved path."""

    return shutil.which("npx") or shutil.which("npx.cmd") or "npx"


__all__ = ["McpError", "McpStdioAdapter", "PROTOCOL_VERSION", "npx_command"]
