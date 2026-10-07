"""供 Agent 工作流使用的内存中 MCP 白名单客户端。"""

from __future__ import annotations

from typing import Protocol

from mcp import Client
from mcp.server import MCPServer

from tara_agent.agent.models import ToolDefinition, ToolName


class AgentToolError(RuntimeError):
    """获准的 MCP 工具无法完成调用时抛出。"""


class ToolGateway(Protocol):
    """同一白名单工具契约的进程内和远程调用边界。"""

    async def list_tools(self) -> list[ToolDefinition]: ...

    async def call(self, tool_name: ToolName, arguments: dict) -> dict: ...


class MCPToolGateway:
    """仅向 Agent 提供已注册且在白名单内的 Tara 工具。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self.allowed_tools = frozenset(ToolName)

    async def list_tools(self) -> list[ToolDefinition]:
        async with Client(self.server) as client:
            listed = await client.list_tools()

        definitions = []
        for tool in listed.tools:
            if tool.name not in self.allowed_tools:
                continue
            definitions.append(
                ToolDefinition(
                    name=ToolName(tool.name),
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                )
            )
        return definitions

    async def call(self, tool_name: ToolName, arguments: dict) -> dict:
        if tool_name not in self.allowed_tools:
            raise AgentToolError(f"Tool is not allowed: {tool_name}")

        async with Client(self.server) as client:
            result = await client.call_tool(tool_name.value, arguments)
        if result.is_error:
            message = " ".join(block.text for block in result.content if hasattr(block, "text"))
            raise AgentToolError(message or f"Tool failed: {tool_name}")
        if result.structured_content is None:
            raise AgentToolError(f"Tool returned no structured result: {tool_name}")
        return result.structured_content
