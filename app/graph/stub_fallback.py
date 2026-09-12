"""Small helper so nodes always have an LLM available."""

from __future__ import annotations

from app.graph.deps import GraphDeps
from app.llm.stub import OfflineStubLLM


def llm_for(deps: GraphDeps):
    return deps.llm if deps.llm is not None else OfflineStubLLM()
