# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.write_note` and `obsidian.write_task` sinks: intents applied to the vault.

The core never calls those tools itself; it applies the intents routed to them through the
sink operations `obsidian.read`, `.apply`, `.restore`, `.restore_status`, `.parent_exists`
and `.identity` (horae-sdk `Sink`, one object for the namespace). For a `note.*` intent the
core computes the bytes; `apply` writes exactly those with `horae_obsidian.writer` (owned
files and blocks only, atomic, compare-and-swap) and returns their digest. A `task.*`
intent (routed by a
`mode: tasks` entry) is applied by `horae_obsidian.tasksink`: one line changed, or one line
appended to the Tasks writer's note. The Tasks writer's notes (`provenance`) are
writer-exclusive: a note intent on one, or its undo, is refused (`tasks_note`). Every
file-system call runs in a worker thread.
"""

import asyncio
from typing import Any

from horae_sdk import Applied, ApplyRequest, Paths, Sink, SinkError

from horae_obsidian import tasksink, writer

TOOL = "obsidian.write_note"
TASK_TOOL = "obsidian.write_task"


class VaultSink(Sink):
    """Applies `note.create`, `note.append_section`, `note.replace_owned_block`,
    `task.create` and `task.update_status`."""

    def __init__(self, tools: list[str], clock: tasksink.Clock = tasksink.now) -> None:
        super().__init__(tools)
        self.clock = clock
        """The current time in the instance time zone (`TZ`); a test passes its own."""

    async def identity(self, paths: Paths) -> str:
        return await asyncio.to_thread(writer.identity, paths)

    async def read(self, paths: Paths, path: str) -> bytes | None:
        return await asyncio.to_thread(writer.read, paths, path)

    async def parent_exists(self, paths: Paths, path: str) -> bool:
        return await asyncio.to_thread(writer.parent_exists, paths, path)

    async def apply(self, request: ApplyRequest) -> Applied:
        intent: dict[str, Any] = request.intent
        kind = intent.get("type")
        if kind in tasksink.TASK_TYPES:
            return await asyncio.to_thread(self._apply_task, request)
        if kind not in writer.NOTE_TYPES:
            raise SinkError("not_supported", f"{TOOL} applies note and task intents only")
        content = request.content
        if content is None:
            raise SinkError("content_missing", "the core sent no note bytes")
        path = intent.get("path")
        writer.note_parts(path)
        await asyncio.to_thread(tasksink.refuse_tasks_note, request.paths, path)

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

    def _apply_task(self, request: ApplyRequest) -> Applied:
        if request.content is not None:
            raise SinkError("intent_invalid", "a task intent comes without note bytes")
        if request.intent["type"] == "task.create":
            return tasksink.create(request.paths, request.params, request.intent, self.clock)
        return tasksink.update_status(request.paths, request.intent, self.clock)

    async def restore(
        self,
        paths: Paths,
        path: str,
        content: bytes | None,
        expected_sha256: str | None,
        force: bool = False,
    ) -> None:
        """`force` is the user's `horae undo --force`, never inferred. Without it, nothing
        differs from before. With it, a note edited outside its blocks is restored block by
        block (`writer.forced_restore`): the user's edit outside them is kept."""
        await asyncio.to_thread(tasksink.refuse_tasks_note, paths, path)
        if force is True:
            await asyncio.to_thread(writer.forced_restore, paths, path, content, expected_sha256)
            return

        def check(before: bytes | None) -> None:
            writer.check_restore(path, before, content)

        await asyncio.to_thread(writer.put, paths, path, content, expected_sha256, check)

    async def restore_status(
        self, paths: Paths, details: dict[str, Any], force: bool = False
    ) -> bool:
        """Undo a `task.update_status` (`details`: what `apply` answered, with `before_line`).
        `force` as for `restore`: only `horae undo --force`."""
        return await asyncio.to_thread(tasksink.restore_status, paths, details, force is True)
