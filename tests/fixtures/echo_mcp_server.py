"""Tiny stdio MCP server fixture for tests.

Exposes two tools:
  - `echo(text)` — returns the text verbatim.
  - `add(a, b)` — returns the sum as a string.

Launched as a subprocess by the manager tests via:
    python -m tests.fixtures.echo_mcp_server
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


server: Server = Server("echo-test")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo the input text.",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        Tool(
            name="add",
            description="Add two numbers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "echo":
        return [TextContent(type="text", text=str(arguments.get("text", "")))]
    if name == "add":
        a = arguments.get("a", 0)
        b = arguments.get("b", 0)
        return [TextContent(type="text", text=str(a + b))]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
