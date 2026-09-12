"""MCP transport (stdio JSON-RPC 2.0)."""

from app.channels.mcp.adapter import McpError, McpStdioAdapter

__all__ = ["McpError", "McpStdioAdapter"]
