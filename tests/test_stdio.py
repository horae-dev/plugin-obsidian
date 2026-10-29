# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it."""

import sys

import anyio
from horae_sdk.testing import read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_obsidian import COMMAND, NAME

# Derived from the plugin's code, so renaming the plugin needs no edit here.
TOOL = f"{NAME}.inbox"


async def serve_and_call() -> tuple[set[str], object, list[object]]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:])
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        replayed = read_result(await client.call_tool(TOOL, {"folder": "news"}))
    return tools, replayed.data, replayed.intents


def test_serves_over_stdio() -> None:
    tools, data, intents = anyio.run(serve_and_call)
    assert TOOL in tools
    assert data == {"folder": "news", "messages": [{"id": "welcome", "subject": "Welcome to news"}]}
    assert intents == [
        {
            "type": "task.create",
            "title": "Reply to Welcome to news",
            "fingerprint": f"{NAME}:reply:welcome",
        }
    ]
