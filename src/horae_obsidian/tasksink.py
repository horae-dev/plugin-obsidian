# SPDX-License-Identifier: Apache-2.0
"""Task intents applied to the vault: `task.update_status` edits one line, `task.create`
appends one line to an owned note (POSIX only).

**`task.update_status`** (`update_status`). The intent's `task` is either a key reported by
`obsidian.open_tasks` (`Task.key`) or the fingerprint of the `task.create` that wrote the
task (its line ends with `^h-<hash>`, found anywhere in the vault: `index.block_of`). The
sink scans the vault and refuses, changing nothing:

- a task it cannot find (`task_not_found`), or several (`task_ambiguous`);
- a task whose key it cannot show unique (`task_ambiguous`): a duplicate, or a vault-scoped
  key (🆔, `^h-`) after a scan that did not read every note (`Scan.known_unique`, fail
  closed);
- closing a recurring task (`recurring_task`: `done` or `cancelled`; Horae never ends a
  recurrence, tick it in Obsidian; `snoozed` and `todo` are fine);
- a note that changed between the scan and the write (`target_changed`): the write is a
  compare-and-swap on the note's SHA-256 (`Document.sha256`), and the line's
  `line_sha256` is checked again in the bytes the writer re-reads before it renames.

Only the task's line changes, through `horae_obsidian.writer.put` (atomic, no symbolic link,
no hard link, case and Unicode twins refused): `done` is `Task.tick` (`✅` dated today in
the instance time zone, the `TZ` the core gives the plugin), `cancelled` `Task.cancel`
(`❌`), `todo` `Task.reopen` (both dates removed), `snoozed` `Task.snooze`: the core's
`until` becomes Horae's reminder, `⏰ YYYY-MM-DD` for a date, `⏰ YYYY-MM-DD HH:MM` for a
date-time (in the instance time zone, rounded up to the minute), before the Tasks date fields.
A task already in the asked state is left as it is (`changed: false`). `Applied.details`
records the note, the line and both line digests; `restore_status` puts the line back (undo).

**`task.create`** (`create`). The line is `horae_sdk.tasks.task_line` (title escaped, link
inert) with Horae's order: `⏰` before `📅`, and `^h-<hash>` last (`render_line`; the same
bytes with horae-sdk 0.2 and 0.3). It is appended to the entry's `note` parameter (trusted;
default `Tasks.assistant.md`), which must be a note Horae owns (`*.assistant.md`,
`not_owned`), created when missing. A task whose block id is already in the vault is not
written again (`changed: false`: the fingerprint deduplicates across runs). The index entry
(`horae_obsidian.index`) is written first, then the line.
"""

import hashlib
import math
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from horae_sdk import Applied, Paths, SinkError
from horae_sdk import tasks as sdk_tasks

from horae_obsidian import writer
from horae_obsidian.index import TaskIndex, block_of
from horae_obsidian.tasks import (
    ENCODING_ERRORS,
    REMINDER,
    Document,
    RecurringTaskError,
    Task,
    json_safe,
    parse,
)
from horae_obsidian.vault import Scan, VaultError, safe_path, scan

TASK_TYPES = ("task.create", "task.update_status")
STATUSES = ("todo", "done", "cancelled", "snoozed")
DEFAULT_NOTE = "Tasks.assistant.md"
Clock = Callable[[], datetime]
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def now() -> datetime:
    """The current time in the instance time zone (the process's `TZ`)."""
    return datetime.now().astimezone()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", ENCODING_ERRORS)).hexdigest()


def _scan(paths: Paths) -> tuple[str, Scan]:
    vault = writer.vault_of(paths)
    if paths.allowed(vault, write=True) is None:
        raise SinkError("outside_vault", "the vault is not an approved paths.write entry")
    try:
        return vault, scan(vault)
    except VaultError as error:
        raise SinkError("unreadable", str(error)) from None


# --- task.update_status -------------------------------------------------------------------


def locate(
    found: Scan, target: str, fingerprints: dict[str, str] | None = None
) -> tuple[Document, Task]:
    """The one task `target` names (its key, or the fingerprint behind its `^h-` block id),
    and its note; `SinkError` unless it is found once and shown unique. A line of Horae's
    own output (`horae_output`: an owned note or block, unless the index shows Horae's Tasks
    writer wrote it) is never a target, as the source never lists it."""
    links = fingerprints or {}
    block = None
    if not target.startswith("obsidian:"):
        try:
            block = block_of(target)
        except ValueError:
            block = None
    matches = [
        (document, task)
        for document in found.documents
        for task in document.tasks
        if (task.key == target or (block is not None and task.horae_block == block))
        and not horae_output(task, links)
    ]
    shown = safe_path(target)
    if not matches:
        raise SinkError("task_not_found", f"no task {shown} in the vault")
    if len(matches) > 1:
        raise SinkError("task_ambiguous", f"{len(matches)} tasks match {shown}: none is changed")
    document, task = matches[0]
    if not found.known_unique(task):
        why = (
            "another task has the same key"
            if not task.key_unique
            else "the scan did not read every note, so its key may not be unique"
        )
        raise SinkError("task_ambiguous", f"{shown}: {why}; nothing is changed")
    return document, task


def reminder_for(until: object, clock: Clock = now) -> str:
    """The `⏰` value for a snooze `until`: the date, or the date-time in the instance time
    zone, rounded up to the minute (`YYYY-MM-DD HH:MM`)."""
    if isinstance(until, str) and _DAY.fullmatch(until):
        try:
            return date.fromisoformat(until).isoformat()
        except ValueError:
            pass
    elif isinstance(until, str) and "T" in until:
        try:
            moment = datetime.fromisoformat(until)
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            local = moment.astimezone(clock().tzinfo)
            extra = local.second + local.microsecond / 1e6
            local = local.replace(second=0, microsecond=0)
            if extra:
                local += timedelta(minutes=math.ceil(extra / 60))
            return local.strftime("%Y-%m-%d %H:%M")
    raise SinkError("intent_invalid", "snoozed needs until: a date or an RFC 3339 date-time")


def new_line(task: Task, status: str, until: object, clock: Clock = now) -> str:
    """`task`'s line in `status` (see the module docstring)."""
    today = clock().date()
    try:
        if status == "done":
            return task.line.text if task.status == "done" else task.tick(today)
        if status == "cancelled":
            return task.line.text if task.status == "cancelled" else task.cancel(today)
    except RecurringTaskError as error:
        raise SinkError("recurring_task", str(error)) from None
    except ValueError as error:
        raise SinkError("task_ambiguous", str(error)) from None
    if status == "todo":
        return task.line.text if task.status == "todo" and not task.done else task.reopen()
    if status == "snoozed":
        value = reminder_for(until, clock)
        reminder = task.reminder
        current = None
        if reminder is not None:
            current = " ".join(p for p in (str(reminder.date or ""), reminder.time or "") if p)
        if task.is_open and current == value and len(_reminders(task)) == 1:
            return task.line.text
        return task.snooze(value)
    raise SinkError("intent_invalid", "status must be todo, done, cancelled or snoozed")


def _reminders(task: Task) -> list[str]:
    return [f.text for f in task.fields if f.kind == "reminder" and f.valid]


def _check_line(document: Document, task: Task, after: bytes) -> Callable[[bytes | None], None]:
    """The writer's last check, on the bytes it re-read: the same note, the same line."""

    def check(before: bytes | None) -> None:
        shown = safe_path(task.path)
        if before is None:
            raise SinkError("target_changed", f"{shown} no longer exists")
        again = parse(before, task.path)
        line = next((t for t in again.tasks if t.line.number == task.line.number), None)
        if line is None or line.line_sha256 != task.line_sha256:
            raise SinkError("target_changed", f"{shown}: the task line changed")
        if before != document.data:
            raise SinkError("target_changed", f"{shown} changed since the scan")
        start, end = task.line.start, task.line.end
        if (
            after[:start] != before[:start]
            or after[len(after) - (len(before) - end) :] != (before[end:])
        ):
            raise SinkError("not_owned", "only the task's line may change")

    return check


def update_status(paths: Paths, intent: dict[str, Any], clock: Clock = now) -> Applied:
    """Apply a `task.update_status` intent (see the module docstring)."""
    target, status = intent.get("task"), intent.get("status")
    if not isinstance(target, str) or status not in STATUSES:
        raise SinkError("intent_invalid", "task.update_status needs a task and a status")
    _, found = _scan(paths)
    document, task = locate(found, target, _fingerprints(paths))
    text = new_line(task, str(status), intent.get("until"), clock)
    details: dict[str, Any] = {
        "path": json_safe(task.path),
        "line": task.line.number,
        "status": status,
        "before_line_sha256": task.line_sha256,
        "before_sha256": document.sha256,
    }
    fingerprint = _fingerprint_of(paths, task)
    if fingerprint is not None:
        details["source_fingerprint"] = fingerprint
    if text == task.line.text:
        return Applied(target, details={**details, "changed": False})
    after = document.replace_line(task, text)
    writer.put(paths, task.path, after, document.sha256, _check_line(document, task, after))
    details.update(
        changed=True,
        after_line_sha256=_sha(text),
        after_sha256=hashlib.sha256(after).hexdigest(),
    )
    if not task.invalid_utf8:  # the undo needs the exact line back
        details["before_line"] = task.line.text
    return Applied(target, details=details)


def _fingerprint_of(paths: Paths, task: Task) -> str | None:
    return None if task.horae_block is None else _fingerprints(paths).get(task.horae_block)


def _fingerprints(paths: Paths) -> dict[str, str]:
    """The index (block id → fingerprint); empty when it cannot be read."""
    try:
        return TaskIndex(writer.vault_of(paths)).read().fingerprints()
    except SinkError:
        return {}


def horae_output(task: Task, fingerprints: dict[str, str]) -> bool:
    """Whether `task` is a line of Horae's own output, never read back as the user's task:
    in an owned block, or in an owned note (`*.assistant.md`) unless it holds a `^h-` block
    id the index maps (a line Horae's Tasks writer wrote for a `task.create`). Otherwise an
    LLM-written note, or a prompt injection it obeyed, would come back as a task."""
    if task.in_owned_block:
        return True
    if not writer.is_owned(task.path):
        return False
    return task.horae_block is None or task.horae_block not in fingerprints


def restore_status(paths: Paths, details: dict[str, Any]) -> bool:
    """Undo a status change: put the line `update_status` replaced back (its `details`),
    if the note still holds the changed line exactly once (`target_changed` otherwise).
    Returns whether it wrote (false: nothing was changed, or it is already restored)."""
    if not details.get("changed"):
        return False
    path, before_line = details.get("path"), details.get("before_line")
    old_sha, new_sha = details.get("before_line_sha256"), details.get("after_line_sha256")
    recorded = isinstance(path, str) and isinstance(before_line, str)
    if not recorded or _sha(str(before_line)) != old_sha:
        raise SinkError("not_restorable", "the change did not record the line it replaced")
    path, before_line = str(path), str(before_line)
    data = writer.read(paths, path)
    if data is None:
        raise SinkError("target_changed", f"{safe_path(path)} no longer exists")
    document = parse(data, path)
    changed = [t for t in document.tasks if t.line_sha256 == new_sha]
    if not changed and any(t.line_sha256 == old_sha for t in document.tasks):
        return False  # already restored
    if len(changed) != 1:
        raise SinkError("target_changed", f"{safe_path(path)}: the task line changed since")
    task = changed[0]
    after = document.replace_line(task, before_line)
    writer.put(paths, path, after, document.sha256, _check_line(document, task, after))
    return True


# --- task.create -------------------------------------------------------------------------


def _link(link: object) -> str | None:
    """The `[link](<destination>)` `task_line` writes (`link_destination`, inert), or `None`:
    no link, or a destination over the SDK's cap (left out, never written raw)."""
    if link is None:
        return None
    if not isinstance(link, str):
        raise ValueError("link must be a string")
    line = sdk_tasks.task_line("x", link=link)
    return line[len("- [ ] x ") :] or None


def render_line(intent: dict[str, Any]) -> str:
    """The Tasks line for a `task.create` intent: `task_line(title, link=...)` (title
    escaped, link inert: `link_destination`), then `⏰ HH:MM`, `📅 YYYY-MM-DD` and `^h-<hash>`
    (horae-sdk 0.3's `task_line(..., fingerprint=...)` order, written here so it holds with
    0.2 too)."""
    title, due, time, link = (intent.get(k) for k in ("title", "due", "time", "link"))
    fingerprint = intent.get("fingerprint")
    if not isinstance(title, str) or not isinstance(fingerprint, str):
        raise SinkError("intent_invalid", "task.create needs a title and a fingerprint")
    if not ((due is None or isinstance(due, str)) and (link is None or isinstance(link, str))):
        raise SinkError("intent_invalid", "due and link must be strings")
    if time is not None and not isinstance(time, str):
        raise SinkError("intent_invalid", "time must be a string")
    try:
        block = block_of(fingerprint)
        sdk_tasks.task_line(title, due=due, time=time)  # the SDK's date and time checks
        parts = [sdk_tasks.task_line(title)]
        destination = _link(link)
    except ValueError as error:
        raise SinkError("intent_invalid", str(error)) from None
    if destination is not None:
        parts.append(destination)
    if time is not None:
        parts.append(f"{REMINDER} {time}")
    if due is not None:
        parts.append(f"{sdk_tasks.DUE} {due}")
    parts.append(f"^{block}")
    return " ".join(parts)


def create(
    paths: Paths, params: dict[str, Any], intent: dict[str, Any], clock: Clock = now
) -> Applied:
    """Apply a `task.create` intent (see the module docstring)."""
    note = params.get("note", DEFAULT_NOTE)
    writer.note_parts(note)
    if not isinstance(note, str) or not writer.is_owned(note):
        raise SinkError("not_owned", f"tasks are created in *{writer.OWNED_SUFFIX} notes only")
    line = render_line(intent)
    fingerprint = str(intent["fingerprint"])
    block = block_of(fingerprint)
    vault, found = _scan(paths)
    existing = [
        (document, task)
        for document in found.documents
        for task in document.tasks
        if task.horae_block == block
    ]
    details: dict[str, Any] = {"block_id": block, "dedupe_complete": found.keys_complete}
    if existing:
        document, task = existing[0]
        return Applied(
            fingerprint,
            details={
                **details,
                "changed": False,
                "path": json_safe(document.path),
                "line": task.line.number,
            },
        )
    index = TaskIndex(vault)
    with index.locked() as entries:
        index.add(entries, fingerprint, clock())
        index.save(entries)
    before = writer.read(paths, note)
    eol = b"\r\n" if before is not None and b"\r\n" in before else b"\n"
    head = before or b""
    if head and not head.endswith((b"\n", b"\r")):
        head += eol
    after = head + line.encode("utf-8") + eol

    def check(current: bytes | None) -> None:
        if current != before:
            raise SinkError("target_changed", f"{safe_path(note)} changed while it was read")

    written = [t for t in parse(after, note).tasks if t.horae_block == block]
    if len(written) != 1 or written[0].line.end != len(after) - len(eol):
        raise SinkError(
            "note_invalid",
            f"{safe_path(note)}: a line appended there would not read as a task (an unclosed"
            " code fence or comment?)",
        )
    writer.put(paths, note, after, writer.sha256(before), check, creating=before is None)
    number = written[0].line.number
    return Applied(
        fingerprint,
        details={**details, "changed": True, "path": note, "line": number, "line_text": line},
    )


__all__ = [
    "DEFAULT_NOTE",
    "TASK_TYPES",
    "create",
    "horae_output",
    "locate",
    "new_line",
    "reminder_for",
    "render_line",
    "restore_status",
    "update_status",
]
