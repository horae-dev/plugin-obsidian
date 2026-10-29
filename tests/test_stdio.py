# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it."""

import sys

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_obsidian import COMMAND, NAME


async def serve_and_list() -> set[str]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:])
    async with Client(server, read_timeout_seconds=30) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


def test_serves_over_stdio() -> None:
    """The server starts and answers; no tool is provided yet (the source and sink are #4)."""
    tools = anyio.run(serve_and_list)
    assert not {tool for tool in tools if tool.startswith(f"{NAME}.")}
