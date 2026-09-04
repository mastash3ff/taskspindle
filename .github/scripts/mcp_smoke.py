"""Start the installed ``taskspindle mcp`` over stdio and check it answers like an MCP server.

This is a packaging test, not a functional one: it proves the wheel's console script launches, the
server completes an MCP initialize, and it advertises the tools it is supposed to. It calls no tool
and touches no repository.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = 16


async def probe() -> list[str]:
    parameters = StdioServerParameters(command="taskspindle", args=["mcp"])
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.list_tools()
        return sorted(tool.name for tool in listing.tools)


def main() -> int:
    names = asyncio.run(asyncio.wait_for(probe(), timeout=120))
    if len(names) != EXPECTED_TOOLS:
        print(f"expected {EXPECTED_TOOLS} tools, got {len(names)}: {', '.join(names)}")
        return 1
    print(f"{len(names)} tools: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
