from __future__ import annotations

import pytest

from app.capabilities.defaults import build_default_registry
from app.config.settings import Settings
from app.graph.checkpointer import build_checkpointer
from app.graph.deps import GraphDeps
from app.graph.runner import GraphRunner
from app.llm.stub import OfflineStubLLM

#: One fully-specified request, shared by every integration test.
#:
#: It lives here because the required-slot set is a moving target (F1 added
#: ``return_date`` and made ``lodging`` required), and five copies drifting apart
#: meant every slot change broke unrelated tests for the wrong reason.
FULL_REQUEST = (
    "从北京市朝阳区望京SOHO出发，2026-09-12 去长城，2026-09-14 回来，"
    "住宿我自己订，一个人，只要建议"
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(llm_offline=True, data_dir=tmp_path)


@pytest.fixture
def runner(settings: Settings) -> GraphRunner:
    deps = GraphDeps(settings=settings, registry=build_default_registry(), llm=OfflineStubLLM())
    return GraphRunner(deps=deps, checkpointer=build_checkpointer(None), repositories=None)


def interrupt_of(events) -> dict | None:
    for event in events:
        if event.type.value == "interrupt":
            return event.data
    return None


def event_of(events, name: str) -> list[dict]:
    return [e.data for e in events if e.type.value == name]
