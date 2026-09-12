"""A minimal MCP server used to exercise the stdio adapter offline.

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout, records every method it
is asked for into ``argv[1]`` (so a test can prove which calls were made), and
prints one non-JSON banner line to prove the client tolerates server noise.
"""

from __future__ import annotations

import json
import sys

TOOLS = ["echo", "boom"]
PROTOCOL_VERSION = "2024-11-05"


def _log(path: str, method: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(method + "\n")


def _reply(message_id: object, result: object) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(message_id: object, code: int, text: str) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}) + "\n"
    )
    sys.stdout.flush()


def main() -> int:
    log_path = sys.argv[1]
    sys.stdout.write("stub mcp server starting\n")  # deliberate stdout noise
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue

        method = message.get("method")
        message_id = message.get("id")
        _log(log_path, str(method))

        if message_id is None:  # a notification; nothing to answer
            continue
        if method == "initialize":
            _reply(message_id, {"protocolVersion": PROTOCOL_VERSION, "serverInfo": {"name": "stub"}})
        elif method == "tools/list":
            _reply(message_id, {"tools": [{"name": name} for name in TOOLS]})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name == "echo":
                _reply(message_id, {"structuredContent": {"echo": params.get("arguments")}})
            elif name == "boom":
                _reply(message_id, {"content": [{"type": "text", "text": "nope"}], "isError": True})
            else:
                _error(message_id, -32602, f"unknown tool: {name}")
        else:
            _error(message_id, -32601, f"unknown method: {method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
