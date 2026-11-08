# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.open_tasks` source: the vault's open tasks, with stable keys.

It scans the vault (`horae_obsidian.vault.scan`, with its budgets: 10,000 notes, 64 MiB,
200,000 tasks) and returns every **open** task: `todo` `[ ]`, `in_progress` `[/]` and any
`custom` status (`[>]`, `[?]`…), which the Tasks plugin treats as to-do when it does not know
the character; `done` (`[x]` `[X]`) and `cancelled` (`[-]`) are closed. Each task carries its
`key` (the `task` a `task.update_status` names), its `source_fingerprint` when Horae wrote it
(the `^h-` block id, mapped by `horae_obsidian.index`), due / scheduled / start dates, the
reminder, tags, the `[link](…)` `task_line` writes, priority and recurrence, with its note,
line and `line_sha256`.

**Horae's own output is not read back** (`tasksink.horae_output`): a line in an owned
block is skipped (counted in `horae_output`), and so is a line in an owned note
(`*.assistant.md`) unless the index records that Horae's Tasks writer wrote exactly that line
in that note for a `task.create` (path and line SHA-256: a `^h-` block id alone is a public
hash and proves nothing). Those are returned with their `source_fingerprint`, e.g.
`github:pr:42`, so a consumer can tell them from the user's tasks and dedupe them against the
live source. Skipped lines do not count as duplicates of the others.
Otherwise an LLM-written note, or a prompt injection it obeyed, would come back the next day
as the user's own task. The sink refuses the same lines.

**Withheld** (fail closed, maintainer decision on #4): a task whose key is not unique
(`duplicate_key`) or not known to be (`uniqueness_unknown`: a vault-scoped 🆔 or `^h-` key
after a scan that did not read every note, `Scan.known_unique`) is not returned as a task but
listed in `withheld` with its note and line, and the sink refuses it. Notes the scan could not
read are in `skipped`, `complete` says whether it read them all.

The vault is the plugin's single approved path (`paths.write`, else `paths.read`, in
`horae.lock`). The source only reads: it writes nothing, not even the index. Everything it
returns is JSON-safe (invalid UTF-8 as U+FFFD, paths with control and bidi characters
escaped). Vault text is untrusted data.
"""

import re
from datetime import date
from typing import Any

from horae_sdk import Paths, PluginError
from horae_sdk import tasks as sdk_tasks

from horae_obsidian.index import TaskIndex
from horae_obsidian.tasks import Task, json_safe
from horae_obsidian.tasksink import eligible_keys, horae_output
from horae_obsidian.vault import VaultError, safe_path, scan

REF = "obsidian.open_tasks"
MAX_LIMIT = 2000
MAX_DESCRIPTION = 2000
"""A longer description is cut (`description_cut`): the output stays well under the host's
message cap."""
_LINK = re.compile(r"\[link\]\(([^\s()<>]*)\)")


def vault_path(paths: Paths) -> str:
    """The vault: the single approved `paths.write` entry, else the single `paths.read`."""
    entries = paths.write or paths.read
    if len(entries) != 1:
        raise PluginError("the obsidian plugin needs exactly one approved vault path")
    return entries[0]


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _link(description: str) -> str | None:
    """The link `task_line` wrote (`[link](<destination>)`), if it is a valid task link."""
    match = _LINK.search(description)
    if match is None:
        return None
    destination = match.group(1)
    return destination if sdk_tasks.is_task_link(destination) else None


def task_json(task: Task, fingerprints: dict[str, str]) -> dict[str, Any]:
    """The source's view of one task (JSON-safe)."""
    reminder = task.reminder
    description = json_safe(task.description)
    cut = len(description) > MAX_DESCRIPTION
    return {
        "key": task.key,
        "source_fingerprint": fingerprints.get(task.horae_block or ""),
        "description": description[:MAX_DESCRIPTION],
        "description_cut": cut,
        "status": task.status,
        "status_char": json_safe(task.status_char),
        "path": safe_path(task.path),
        "line": task.line.number,
        "line_sha256": task.line_sha256,
        "heading": json_safe(task.heading)[:200] if task.heading is not None else None,
        "due": _day(task.due),
        "scheduled": _day(task.scheduled),
        "start": _day(task.start),
        "reminder": (
            {"date": _day(reminder.date), "time": reminder.time} if reminder is not None else None
        ),
        "tags": [json_safe(tag) for tag in task.tags],
        "link": _link(task.description),
        "priority": task.priority,
        "recurrence": task.recurrence,
        "recurring": task.recurrence is not None,
        "id": task.id,
        "block_id": task.block_id,
    }


def _order(task: Task) -> tuple[date, str, int]:
    return (task.due or date.max, task.path, task.line.number)


def open_tasks(paths: Paths, *, limit: int = 500) -> dict[str, Any]:
    """The open tasks of the vault (see the module docstring), at most `limit`, by due date
    (none last), then note and line; `more` counts those left out."""
    if not 1 <= limit <= MAX_LIMIT:
        raise PluginError(f"limit must be between 1 and {MAX_LIMIT}")
    vault = vault_path(paths)
    try:
        found = scan(vault)
    except VaultError as error:
        raise PluginError(str(error)) from None
    try:
        links = TaskIndex(vault).read().links
        index_error = None
    except Exception as error:  # the index only adds fingerprints: never fails the source
        links, index_error = {}, getattr(error, "kind", type(error).__name__)
    fingerprints = {block: link.fingerprint for block, link in links.items()}
    tasks: list[Task] = []
    withheld: list[dict[str, Any]] = []
    own_output = 0
    counts = eligible_keys(found, links)
    for document in found.documents:
        for task in document.tasks:
            if not task.is_open:
                continue
            if horae_output(task, links):
                own_output += 1
                continue
            if found.known_unique(task, counts):
                tasks.append(task)
                continue
            withheld.append(
                {
                    "key": task.key,
                    "path": safe_path(task.path),
                    "line": task.line.number,
                    "reason": ("duplicate_key" if counts[task.key] != 1 else "uniqueness_unknown"),
                }
            )
    tasks.sort(key=_order)
    shown = tasks[:limit]
    return {
        "tasks": [task_json(task, fingerprints) for task in shown],
        "more": len(tasks) - len(shown),
        "withheld": withheld[:MAX_LIMIT],
        "withheld_more": max(0, len(withheld) - MAX_LIMIT),
        "horae_output": own_output,
        "complete": found.keys_complete,
        "truncated": found.truncated,
        "skipped": [skipped.to_json() for skipped in found.skipped[:MAX_LIMIT]],
        "index": "ok" if index_error is None else f"unavailable ({index_error})",
    }


__all__ = ["MAX_LIMIT", "REF", "open_tasks", "task_json", "vault_path"]
