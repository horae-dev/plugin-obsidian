# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.write_note` sink: the core's note bytes, written into the vault.

The core never calls `obsidian.write_note` itself; it applies the intents routed to it
through the sink operations `obsidian.read`, `.apply`, `.restore`, `.parent_exists` and
`.identity` (horae-sdk `Sink`). For a `note.*` intent the core computes the bytes; `apply`
writes exactly those with `horae_obsidian.writer` (owned files and blocks only, atomic,
compare-and-swap) and returns their digest. Every file-system call runs in a worker thread.
"""

import asyncio
from typing import Any

from horae_sdk import Applied, ApplyRequest, Paths, Sink, SinkError

from horae_obsidian import writer

TOOL = "obsidian.write_note"


class VaultSink(Sink):
    """Applies `note.create`, `note.append_section` and `note.replace_owned_block`."""

    def identity(self, paths: Paths) -> str:
        return writer.identity(paths)

    async def read(self, paths: Paths, path: str) -> bytes | None:
        return await asyncio.to_thread(writer.read, paths, path)

    async def parent_exists(self, paths: Paths, path: str) -> bool:
        return await asyncio.to_thread(writer.parent_exists, paths, path)

    async def apply(self, request: ApplyRequest) -> Applied:
        intent: dict[str, Any] = request.intent
        if intent.get("type") not in writer.NOTE_TYPES:
            raise SinkError("not_supported", f"{TOOL} applies note intents only")
        content = request.content
        if content is None:
            raise SinkError("content_missing", "the core sent no note bytes")
        path = intent.get("path")
        writer.note_parts(path)

        def check(before: bytes | None) -> None:
            writer.check_apply(intent, before, content)

        await asyncio.to_thread(
            lambda: writer.put(
                request.paths,
                path,
                content,
                request.expected_before_sha256,
                check,
                creating=intent["type"] == "note.create",
            )
        )
        return Applied.wrote(str(path), content)

    async def restore(
        self, paths: Paths, path: str, content: bytes | None, expected_sha256: str | None
    ) -> None:
        def check(before: bytes | None) -> None:
            writer.check_restore(path, before, content)

        await asyncio.to_thread(writer.put, paths, path, content, expected_sha256, check)
