# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.open_tasks` source: the vault's open tasks, with stable keys.

It scans the vault (`horae_obsidian.vault.scan`, with its budgets: 10,000 notes, 64 MiB,
200,000 tasks) and returns every **open** task: by default `todo` `[ ]` and `in_progress`
`[/]`; the `open_statuses` parameter lists the status characters that count as open (a
theme's `[>]`, `[?]`… are not open unless listed; they are counted in `not_open_statuses`).
`[x]` `[X]` `[-]` are always closed. Each task carries its `key` (the `task` a
`task.update_status` names), its `source_fingerprint` when it is a task Horae wrote (the `^h-`
block id, mapped by `horae_obsidian.index`), due / scheduled / start dates, the reminder,
`snoozed_until` and `snoozed` (a `⏰` date or date-time still in the future: consumers decide
what a snooze means, the reminders pack skips them), tags, the `[link](…)` `task_line` writes
(Horae's tasks only: a link the user wrote is not reported), priority and recurrence, with
its note, line and `line_sha256`.

**Horae's own output is not read back** (`provenance.Provenance.hidden`): a line in an owned
block, a line of an owned note (`*.assistant.md`, LLM-written) and a line of the Tasks
writer's note that the index does not record are skipped, and listed in `hidden` with their
note, line and reason (counted in `horae_output`), so nothing disappears silently. A task of
the Tasks writer's note the index records is Horae's task, edited in Obsidian or not: it is
returned (with its `source_fingerprint`), and when edited also listed in
`edited_horae_tasks`. Hidden lines do not count as duplicates of the others.

**Withheld** (fail closed, maintainer decision on #4): a task whose key is not unique
(`duplicate_key`) or not known to be (`uniqueness_unknown`: a vault-scoped 🆔 or `^h-` key
after a scan that did not read every note, `Scan.known_unique`) is not returned as a task but
listed in `withheld` with its note and line, and the sink refuses it. Notes the scan could not
read are in `skipped`, `complete` says whether it read them all. None of these fails the run.

**Bounded**: at most `limit` tasks and `MAX_OUTPUT_BYTES` of JSON (`output_cut`), `more`
counting the tasks left out; each list at most `MAX_LIMIT` entries.

The vault is the plugin's single approved path (`paths.write`, else `paths.read`, in
`horae.lock`). The source only reads: it writes nothing, not even the index. Everything it
returns is JSON-safe (invalid UTF-8 as U+FFFD, paths with control and bidi characters
escaped). Vault text is untrusted data.
"""

import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

from horae_sdk import Paths, PluginError
from horae_sdk import tasks as sdk_tasks

from horae_obsidian import clock as instance
from horae_obsidian.index import Entries, TaskIndex
from horae_obsidian.provenance import Provenance, View
from horae_obsidian.tasks import STATUSES, Task, json_safe
from horae_obsidian.vault import VaultError, safe_path, scan

REF = "obsidian.open_tasks"
MAX_LIMIT = 2000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
"""At most this much JSON (ASCII-escaped) in a result: past it, `output_cut` and `more`."""
DEFAULT_OPEN_STATUSES = " /"
"""The status characters that count as open: to do and in progress."""
MAX_OPEN_STATUSES = 32
CLOSED = "xX-"
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
    """The link `task_line` wrote (`[link](<destination>)`, the last one), if valid."""
    found = _LINK.findall(description)
    if not found:
        return None
    destination = found[-1]
    return destination if sdk_tasks.is_task_link(destination) else None


def snoozed_until(task: Task, now: datetime) -> str | None:
    """The task's `⏰` date (`YYYY-MM-DD`) or date-time (ISO 8601 in the instance time zone)
    when it is still in the future: a snooze. A bare `⏰ HH:MM` (the time on the due date)
    is a reminder, not a snooze."""
    reminder = task.reminder
    if reminder is None or reminder.date is None:
        return None
    if reminder.time is None:
        return reminder.date.isoformat() if reminder.date > now.date() else None
    hours, minutes = (int(part) for part in reminder.time.split(":"))
    moment = instance.localize(datetime.combine(reminder.date, time(hours, minutes)))
    return moment.isoformat(timespec="minutes") if moment > now else None


def task_json(task: Task, view: View, now: datetime) -> dict[str, Any]:
    """The source's view of one task (JSON-safe)."""
    reminder = task.reminder
    description = json_safe(task.description)
    cut = len(description) > MAX_DESCRIPTION
    fingerprint = view.source_fingerprint(task)
    until = snoozed_until(task, now)
    return {
        "key": task.key,
        "source_fingerprint": fingerprint,
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
        "snoozed_until": until,
        "snoozed": until is not None,
        "tags": [json_safe(tag) for tag in task.tags],
        "link": _link(task.description) if fingerprint is not None else None,
        "priority": task.priority,
        "recurrence": task.recurrence,
        "recurring": task.recurrence is not None,
        "id": task.id,
        "block_id": task.block_id,
        "edited": view.provenance.edited(task),
    }


def _order(task: Task) -> tuple[date, str, int]:
    return (task.due or date.max, task.path, task.line.number)


def _open_statuses(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_OPEN_STATUSES
        or any(not char.isprintable() or char in CLOSED for char in value)
    ):
        raise PluginError(
            f"open_statuses is a string of 1 to {MAX_OPEN_STATUSES} printable status"
            f" characters, none of {CLOSED!r}"
        )
    return value


def _size(value: object) -> int:
    return len(json.dumps(value)) + 2  # ASCII escapes: the worst case, and a separator


def open_tasks(
    paths: Paths,
    *,
    limit: int = 500,
    open_statuses: str = DEFAULT_OPEN_STATUSES,
    clock: Callable[[], datetime] = instance.now,
) -> dict[str, Any]:
    """The open tasks of the vault (see the module docstring), at most `limit`, by due date
    (none last), then note and line; `more` counts those left out."""
    if not 1 <= limit <= MAX_LIMIT:
        raise PluginError(f"limit must be between 1 and {MAX_LIMIT}")
    opened = _open_statuses(open_statuses)
    vault = vault_path(paths)
    try:
        found = scan(vault)
    except VaultError as error:
        raise PluginError(str(error)) from None
    try:
        entries = TaskIndex(vault).read()
        index_error = None
    except Exception as error:  # the index only adds fingerprints: never fails the source
        entries, index_error = Entries(), getattr(error, "kind", type(error).__name__)
    view = View.of(found, Provenance(entries))
    tasks: list[Task] = []
    withheld: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    edited: list[dict[str, Any]] = []
    not_open: Counter[str] = Counter()
    for _, task in view.tasks():
        if task.status_char not in opened:
            if task.status_char not in STATUSES:
                not_open[json_safe(task.status_char)] += 1
            continue
        where = {"path": safe_path(task.path), "line": task.line.number}
        reason = view.provenance.hidden(task)
        if reason is not None:
            hidden.append({**where, "reason": reason})
            continue
        if view.provenance.edited(task):
            edited.append(
                {"key": task.key, **where, "source_fingerprint": view.source_fingerprint(task)}
            )
        if view.known_unique(task):
            tasks.append(task)
            continue
        reason = "duplicate_key" if view.counts[task.key] != 1 else "uniqueness_unknown"
        withheld.append({"key": task.key, **where, "reason": reason})
    tasks.sort(key=_order)
    result: dict[str, Any] = {
        "tasks": [],
        "more": 0,
        "output_cut": False,
        "open_statuses": opened,
        "withheld": withheld[:MAX_LIMIT],
        "withheld_more": max(0, len(withheld) - MAX_LIMIT),
        "hidden": hidden[:MAX_LIMIT],
        "hidden_more": max(0, len(hidden) - MAX_LIMIT),
        "horae_output": len(hidden),
        "edited_horae_tasks": edited[:MAX_LIMIT],
        "edited_more": max(0, len(edited) - MAX_LIMIT),
        "not_open_statuses": dict(sorted(not_open.items())),
        "complete": found.keys_complete,
        "truncated": found.truncated,
        "skipped": [skipped.to_json() for skipped in found.skipped[:MAX_LIMIT]],
        "index": "ok" if index_error is None else f"unavailable ({index_error})",
    }
    budget = MAX_OUTPUT_BYTES - _size(result)
    moment = clock()
    shown: list[dict[str, Any]] = []
    for task in tasks[:limit]:
        item = task_json(task, view, moment)
        budget -= _size(item)
        if budget < 0:
            result["output_cut"] = True
            break
        shown.append(item)
    result["tasks"] = shown
    result["more"] = len(tasks) - len(shown)
    return result


__all__ = [
    "DEFAULT_OPEN_STATUSES",
    "MAX_LIMIT",
    "MAX_OUTPUT_BYTES",
    "REF",
    "open_tasks",
    "snoozed_until",
    "task_json",
    "vault_path",
]
