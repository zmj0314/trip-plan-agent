"""Offline demo / acceptance harness for the M0 flow.

Runs "clarify -> information gate -> plan preview -> consent gate -> execute"
with no network and no credentials, which is exactly the M0 acceptance
criterion. Everything the LLM would do is served by the deterministic stub.

Usage:
    .venv\\Scripts\\python.exe -m app.cli            # scripted demo
    .venv\\Scripts\\python.exe -m app.cli --chat     # interactive
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from app.capabilities.defaults import build_default_registry
from app.config.settings import Settings
from app.graph.checkpointer import build_checkpointer
from app.graph.deps import GraphDeps
from app.graph.runner import GraphRunner
from app.llm.stub import OfflineStubLLM


def build_demo_runner(*, data_dir: Path | None = None) -> GraphRunner:
    settings = Settings(llm_offline=True, data_dir=data_dir or Path(tempfile.mkdtemp(prefix="travel-agent-")))
    registry = build_default_registry()
    resolver = None
    try:
        from app.channels.bootstrap import build_resolver

        resolver = build_resolver(settings, registry, db=None)
    except Exception as exc:  # pragma: no cover - channels are optional
        print(f"[warn] channels unavailable ({type(exc).__name__}); capabilities will degrade")
    deps = GraphDeps(settings=settings, registry=registry, resolver=resolver, llm=OfflineStubLLM())
    return GraphRunner(deps=deps, checkpointer=build_checkpointer(None), repositories=None)


def show(events) -> None:
    for event in events:
        data = event.data
        if event.type.value == "interrupt":
            print(f"  [INTERRUPT] kind={data.get('kind')}")
            if data.get("kind") == "question":
                for q in data.get("questions", []):
                    print(f"             ? {q}")
            elif data.get("kind") == "gate2_full":
                print(f"             preview: {data.get('preview')}")
                if data.get("assumptions"):
                    print(f"             假设: {data['assumptions']}")
        elif event.type.value == "scope_rejected":
            print(f"  [SCOPE] {data.get('text')}")
        elif event.type.value == "degradation_notice":
            print(f"  [DEGRADED] {data.get('capability_id')} <- {data.get('reason')}")
        elif event.type.value == "state_update":
            brief = {k: v for k, v in data.items() if k in {"phase", "scope", "missing", "gate_ok", "actions"}}
            print(f"  [STATE] {brief}")
        elif event.type.value == "done":
            print(f"  [DONE] {data}")
        else:
            print(f"  [{event.type.value}] {data}")


async def scripted_demo() -> int:
    runner = build_demo_runner()
    session = await runner.start()
    sid = session["session_id"]
    print(f"session: {sid}\n")

    print("> 我 2026-09-12 想去长城")
    show(await runner.send(sid, "我 2026-09-12 想去长城"))

    print('\n> 从北京市朝阳区望京SOHO出发，一个人，只要建议，其余你定')
    show(await runner.resume(sid, {"text": "从北京市朝阳区望京SOHO出发，一个人，只要建议，其余你定"}))

    snapshot = await runner.snapshot(sid)
    print(f"\n  待确认计划: {snapshot.get('current_plan_version_id')} hash={str(snapshot.get('plan_hash'))[:12]}…")

    print("\n> [用户点击 同意]")
    show(await runner.resume(sid, {"decision": "approve", "plan_hash": snapshot.get("plan_hash")}))

    final = await runner.snapshot(sid)
    print(f"\n终态: phase={final.get('phase')} actions={final.get('actions')}")
    return 0 if final.get("phase") == "DONE" else 1


async def chat() -> int:
    runner = build_demo_runner()
    sid = (await runner.start())["session_id"]
    print(f"session {sid} — 输入 'exit' 退出")
    pending = None
    while True:
        text = input("你> ").strip()
        if text in {"exit", "quit"}:
            return 0
        if pending and pending.get("kind") == "question":
            events = await runner.resume(sid, {"text": text})
        elif pending and pending.get("kind") == "gate2_full":
            decision = "approve" if text in {"y", "yes", "同意", "好"} else "reject"
            snap = await runner.snapshot(sid)
            events = await runner.resume(sid, {"decision": decision, "plan_hash": snap.get("plan_hash")})
        else:
            events = await runner.send(sid, text)
        show(events)
        pending = (await runner.snapshot(sid)).get("pending_interrupt")


def main() -> int:
    parser = argparse.ArgumentParser(description="travel-plan agent M0 demo")
    parser.add_argument("--chat", action="store_true", help="interactive mode")
    args = parser.parse_args()
    return asyncio.run(chat() if args.chat else scripted_demo())


if __name__ == "__main__":
    raise SystemExit(main())
