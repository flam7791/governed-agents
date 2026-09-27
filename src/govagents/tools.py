"""Tool registry: Python functions and MCP servers behind one interface.

Every tool carries an action class (read, write_internal, external), which the policy engine
uses. Python tools declare it explicitly. MCP tools are classified from the server's own
annotations: a tool marked read-only is "read"; anything else is treated as "external" (the
most restrictive class) unless the scenario configuration says otherwise. Unknown tools
therefore start locked down, and are opened deliberately.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

from .models import ToolSpec

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 4000  # bounds what a tool result can add to the context (and to cost)


@dataclass
class PythonTool:
    spec: ToolSpec
    fn: Callable[..., object]

    async def call(self, arguments: dict) -> str:
        result = await asyncio.to_thread(self.fn, **arguments)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


@dataclass
class McpTool:
    spec: ToolSpec
    client: object  # an open mcp.Client

    async def call(self, arguments: dict) -> str:
        result = await self.client.call_tool(self.spec.name, arguments)
        text = "\n".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            raise ToolFailed(text or "MCP tool error")
        return text


class ToolFailed(RuntimeError):
    """A tool ran but failed; the message goes back to the agent."""


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, PythonTool | McpTool] = {}
        self._stack = AsyncExitStack()

    def add(self, tool: PythonTool | McpTool) -> None:
        if tool.spec.name in self.tools:
            raise ValueError(f"Duplicate tool name {tool.spec.name}")
        self.tools[tool.spec.name] = tool

    def spec(self, name: str) -> ToolSpec | None:
        tool = self.tools.get(name)
        return tool.spec if tool else None

    def specs_for(self, names: tuple[str, ...]) -> list[ToolSpec]:
        """The tools an agent may see: its own list, restricted to what is available."""
        return [self.tools[n].spec for n in names if n in self.tools]

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run a tool. Returns (text, is_error). Errors are reported, never raised."""
        tool = self.tools.get(name)
        if tool is None:
            return f"Unknown tool {name}.", True
        try:
            text = await tool.call(arguments)
            is_error = False
        except TypeError as exc:  # wrong or missing arguments
            text, is_error = f"Invalid arguments for {name}: {exc}", True
        except Exception as exc:
            text, is_error = f"{name} failed: {exc}", True
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " [truncated]"
        return text, is_error

    async def connect_mcp(
        self, name: str, server, action_overrides: dict[str, str] | None = None
    ) -> list[str]:
        """Connect an MCP server (StdioServerParameters, URL or in-process server)."""
        from mcp import Client

        client = await self._stack.enter_async_context(Client(server))
        overrides = action_overrides or {}
        added = []
        for tool in (await client.list_tools()).tools:
            annotations = tool.annotations
            read_only = bool(annotations and annotations.read_only_hint)
            action = overrides.get(tool.name) or ("read" if read_only else "external")
            spec = ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
                action_class=action,
                source=f"mcp:{name}",
            )
            self.add(McpTool(spec, client))
            added.append(tool.name)
        log.info("MCP server %s: %d tools", name, len(added))
        return added

    async def aclose(self) -> None:
        await self._stack.aclose()


def python_tool(name: str, description: str, action_class: str, schema: dict):
    """Decorator: turn a function into a PythonTool with a declared action class."""

    def wrap(fn):
        spec = ToolSpec(
            name=name,
            description=description,
            input_schema={"type": "object", **schema},
            action_class=action_class,
        )
        return PythonTool(spec, fn)

    return wrap
