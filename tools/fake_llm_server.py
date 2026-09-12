"""A minimal OpenAI-compatible server for exercising the local-LLM path.

Why this exists: "configured" and "reachable" are different things, and
proving DM-3 end-to-end should not require downloading a multi-hundred-megabyte
GGUF first. This speaks just enough of the protocol that ``llama-server``
speaks -- ``POST /v1/chat/completions`` and ``GET /v1/models`` -- so the whole
provider path (profile -> headers -> client -> structured parse -> plan) can be
verified offline.

It is a **test double**, not a model: responses are canned and matched on the
prompt's task segment.

    .venv\\Scripts\\python.exe tools\\fake_llm_server.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_ID = "fake-qwen-local"


def _respond(prompt: str) -> dict[str, Any]:
    """Pick a canned payload that satisfies the schema the node asked for."""

    if "只输出一个 JSON 对象" in prompt:
        return {"ok": True, "note": "pong"}

    if "抽取旅行需求槽位" in prompt or "slot_patch" in prompt:
        patch: dict[str, Any] = {}
        for marker, slot in (
            ("长城", "destination"),
            ("成都", "destination"),
            ("杭州", "destination"),
        ):
            if marker in prompt:
                patch["destination"] = marker
                break
        address = re.search(r"从\s*([^\s，,。]{2,20}?)\s*(?:出发|到|去)", prompt)
        if address:
            patch["origin_address"] = address.group(1)
        date = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", prompt)
        if date:
            patch["depart_date"] = f"{date.group(1)}-{int(date.group(2)):02d}-{int(date.group(3)):02d}"
        for marker, count in (("一个人", 1), ("两个人", 2), ("三个人", 3)):
            if marker in prompt:
                patch["travelers"] = {"count": count, "types": ["adult"]}
                break
        if "随便" in prompt or "你定" in prompt:
            patch["intent"] = "advice_only"
        return {
            "scope": "in_scope",
            "scope_reason": "local test double",
            "is_injection": False,
            "slot_patch": patch,
            "intent": "advice_only",
            "questions": [],
        }

    if "追问" in prompt or "questions" in prompt:
        return {"questions": ["你的出发地是哪里？", "计划哪天出发？", "一共几个人？"]}
    if "计划预览" in prompt:
        return {"text": "已根据你的需求排出方案：从望京SOHO出发，当天往返长城，含接驳与门票预约提醒。"}
    if "提醒" in prompt:
        return {"items": ["出发前一天确认长城预约名额。", "早起查看八达岭方向路况。"]}
    if "理由" in prompt:
        return {"text": "该方案在时间与费用之间取得平衡。"}
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than default
        pass

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/").endswith("/v1/models"):
            self._send({"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})
            return
        self._send({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self.path.rstrip("/").endswith("/v1/chat/completions"):
            self._send({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send({"error": {"message": "invalid json"}}, status=400)
            return

        prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", []))
        content = json.dumps(_respond(prompt), ensure_ascii=False)
        self._send(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model") or MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": max(1, len(prompt) // 4),
                    "completion_tokens": max(1, len(content) // 4),
                    "total_tokens": max(2, (len(prompt) + len(content)) // 4),
                },
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="fake OpenAI-compatible LLM server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake LLM listening on http://{args.host}:{args.port}/v1  (model={MODEL_ID})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
