"""Stdio MCP server with a deliberately-slow `slow_call` for drain tests.

Launched as `python -m tests.fixtures.slow_mcp_server`.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


server: Server = Server("slow-test")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="slow_call",
            description="Sleep for `seconds` then return ok.",
            inputSchema={
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
                "required": ["seconds"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "slow_call":
        await asyncio.sleep(float(arguments.get("seconds", 0.1)))
        return [TextContent(type="text", text="ok")]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
