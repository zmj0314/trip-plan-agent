"""MCP stdio adapter, exercised against a real subprocess (framework §6.10)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.capabilities.contract import ChannelKind, ResultStatus
from app.capabilities.registry import CapabilityBinding
from app.channels.mcp.adapter import McpError, McpStdioAdapter

STUB = Path(__file__).with_name("mcp_server_stub.py")


def make_adapter(log_path: Path, **kwargs) -> McpStdioAdapter:
    return McpStdioAdapter(
        adapter_id="stub",
        command=sys.executable,
        args=[str(STUB), str(log_path)],
        connect_timeout=10.0,
        **kwargs,
    )


def methods_called(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
async def adapter(tmp_path):
    instance = make_adapter(tmp_path / "calls.log")
    yield instance
    await instance.aclose()


async def test_handshake_lists_the_tools(adapter):
    assert await adapter.start() is True
    assert adapter.tools == frozenset({"echo", "boom"})
    assert adapter.supports("any.capability", "echo") is True
    assert adapter.supports("any.capability", "nope") is False


async def test_a_dead_server_is_replaced_on_the_next_call(adapter):
    """A crashed MCP server must not poison every later call.

    Before this, a dead child kept its HEALTHY badge and its slot: the guard
    checked ``_proc is not None`` instead of whether the process was still
    running, so every subsequent call replayed the same failure forever.
    """

    binding = CapabilityBinding(adapter_id="stub", remote_name="echo")
    assert (await adapter.invoke(binding, {"n": 1}, timeout=5.0)).status is ResultStatus.OK

    proc = adapter._proc
    first_pid = proc.pid
    proc.kill()
    await proc.wait()
    assert adapter._dead() is True

    healed = await adapter.invoke(binding, {"n": 2}, timeout=10.0)
    assert healed.status is ResultStatus.OK
    assert healed.data == {"echo": {"n": 2}}
    assert adapter._proc.pid != first_pid, "a fresh server should have been started"


async def test_a_tool_error_does_not_restart_the_server(adapter):
    """Only a transport failure means the server died.

    Reconnecting on a healthy server's "no" would burn a cold start for nothing
    -- and for `npx`-hosted servers that cold start is up to 90 seconds.
    """

    binding = CapabilityBinding(adapter_id="stub", remote_name="boom")
    with pytest.raises(McpError):
        await adapter.invoke(binding, {}, timeout=5.0)

    assert adapter._dead() is False
    assert adapter._state.value == "healthy"


async def test_tool_call_returns_structured_content(adapter):
    result = await adapter.invoke(
        CapabilityBinding(adapter_id="stub", remote_name="echo"),
        {"a": 1},
        timeout=5.0,
    )

    assert result.status is ResultStatus.OK
    assert result.data == {"echo": {"a": 1}}
    assert result.provenance is not None
    assert result.provenance.channel is ChannelKind.MCP
    assert result.provenance.provider == "stub"


async def test_param_and_result_maps_translate_at_the_boundary(adapter):
    """§6.10.1: the orchestration layer never sees a provider's field names."""

    binding = CapabilityBinding(
        adapter_id="stub",
        remote_name="echo",
        param_map={"origin": "from", "destination": "to"},
    )
    result = await adapter.invoke(binding, {"origin": "Beijing", "destination": "Shanghai"}, timeout=5.0)

    assert result.data == {"echo": {"from": "Beijing", "to": "Shanghai"}}


async def test_a_tool_level_error_is_raised_not_swallowed(adapter):
    with pytest.raises(McpError):
        await adapter.invoke(
            CapabilityBinding(adapter_id="stub", remote_name="boom"),
            {},
            timeout=5.0,
        )


async def test_unknown_tool_raises_a_business_error(adapter):
    with pytest.raises(McpError) as caught:
        await adapter.invoke(
            CapabilityBinding(adapter_id="stub", remote_name="missing"),
            {},
            timeout=5.0,
        )
    assert caught.value.kind == "business"
    assert caught.value.retryable is False


async def test_health_never_issues_a_business_call(tmp_path):
    """DG-6: a liveness probe must not spend the provider's quota."""

    log_path = tmp_path / "calls.log"
    instance = make_adapter(log_path)
    try:
        status = await instance.health()
    finally:
        await instance.aclose()

    assert status.state.value == "healthy"
    assert "tools/call" not in methods_called(log_path)


async def test_a_missing_server_degrades_instead_of_raising(tmp_path):
    """§6.10.2: a provider that is down must not break the turn."""

    instance = McpStdioAdapter(
        adapter_id="absent",
        command="definitely-not-a-real-mcp-server",
        connect_timeout=5.0,
    )
    try:
        result = await instance.invoke(
            CapabilityBinding(adapter_id="absent", remote_name="anything"),
            {},
            timeout=1.0,
        )
    finally:
        await instance.aclose()

    assert result.status is ResultStatus.UNAVAILABLE
    assert result.degradation.reason
    assert result.warnings
