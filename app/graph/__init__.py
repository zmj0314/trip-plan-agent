"""Orchestration layer (L3).

The graph owns control flow and nothing else: every decision it makes is either
a call into the deterministic policy layer (L4) or a capability call through the
resolver (L6). It never imports ``app.channels`` or an MCP SDK (guard `依赖方向`).
"""
