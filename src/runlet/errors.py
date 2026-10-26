"""Errors returned through MCP as tool failures."""

from mcp.server.mcpserver.exceptions import ToolError


class SandboxError(ToolError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")
