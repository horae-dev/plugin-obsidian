# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it, and its
sink operations answer as the core calls them (the SDK's harness, then a real subprocess)."""

import base64
import hashlib
import sys
from pathlib import Path
from typing import Any

import anyio
from horae_sdk.testing import operation, read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_obsidian import COMMAND, NAME, build

OPERATIONS = {f"{NAME}.{op}" for op in ("read", "apply", "restore", "parent_exists", "identity")}
NOTE = "Daily/2026-10-29.assistant.md"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wire_paths(vault: Path) -> dict[str, list[str]]:
    return {"read": [], "write": [str(vault)]}


def test_sink_operations_through_the_sdk_harness(tmp_path: Path) -> None:
    plugin, paths = build(), wire_paths(tmp_path)
    assert plugin.sinks[NAME].tools == (f"{NAME}.write_note",)

    async def scenario() -> list[dict[str, Any]]:
        intent = {"type": "note.create", "path": NOTE, "content": "# Brief"}
        return [
            await operation(plugin, f"{NAME}.parent_exists", {"paths": paths, "path": NOTE}),
            await operation(
                plugin,
                f"{NAME}.apply",
                {"paths": paths, "intent": intent, "content": b64(b"# Brief\n"), "tool": "x"},
            ),
            await operation(plugin, f"{NAME}.read", {"paths": paths, "path": NOTE}),
            await operation(
                plugin,
                f"{NAME}.apply",
                {"paths": paths, "intent": {**intent, "path": "Notes/user.md"}, "content": "eA=="},
            ),
            await operation(
                plugin,
                f"{NAME}.restore",
                {
                    "paths": paths,
                    "path": NOTE,
                    "content": None,
                    "expected_sha256": sha(b"# Brief\n"),
                },
            ),
            await operation(plugin, f"{NAME}.identity", {"paths": paths}),
        ]

    exists, applied, read, refused, restored, identity = anyio.run(scenario)
    assert exists == {"exists": False}
    assert applied == {"target": NOTE, "after_sha256": sha(b"# Brief\n")}
    assert read == {"content": b64(b"# Brief\n")}
    assert refused["error"]["kind"] == "not_owned"
    assert restored == {}
    assert not (tmp_path / NOTE).exists()
    assert identity["identity"].startswith("vault:")


async def serve(vault: Path) -> tuple[set[str], dict[str, Any], bytes]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:])
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        intent = {"type": "note.create", "path": NOTE, "content": "hi"}
        arguments = {"paths": wire_paths(vault), "intent": intent, "content": b64(b"hi\n")}
        result = read_result(await client.call_tool(f"{NAME}.apply", arguments))
        assert isinstance(result.data, dict)
        return tools, result.data, (vault / NOTE).read_bytes()


def test_serves_the_sink_over_stdio(tmp_path: Path) -> None:
    """The sink operations are listed (not provided: no pipeline step can call them) and
    `apply` writes the core's bytes."""
    tools, applied, written = anyio.run(serve, tmp_path)
    assert {tool for tool in tools if tool.startswith(f"{NAME}.")} == OPERATIONS
    assert applied == {"target": NOTE, "after_sha256": sha(b"hi\n")}
    assert written == b"hi\n"
