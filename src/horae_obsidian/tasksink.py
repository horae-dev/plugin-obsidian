# SPDX-License-Identifier: Apache-2.0
"""Task intents applied to the vault: `task.update_status` edits one line, `task.create`
appends one line to the Tasks writer's note (POSIX only).

**`task.update_status`** (`update_status`). The intent's `task` is either a key reported by
`obsidian.open_tasks` (`Task.key`) or the fingerprint of the `task.create` that wrote the
task (its line ends with `^h-<hash>`: `index.block_of`). A fingerprint, or an
`obsidian:horae:` key, is looked up in its writer note first (the index's path: one note
read, `_quick`); otherwise the sink scans the vault. It refuses, changing nothing:

- a task it cannot find (`task_not_found`), or several (`task_ambiguous`);
- a task whose key it cannot show unique (`task_ambiguous`): a duplicate, or a vault-scoped
  key (🆔, `^h-`) after a scan that did not read every note (`Scan.known_unique`, fail
  closed);
- snoozing a closed task (`task_closed`: reopen it with `todo` first);
- a note that changed under it (`target_changed`): once located, the note alone is read
  again, the task found again by its key and line SHA-256, and that fresh SHA-256 is the
  compare-and-swap value of the write (`writer.put`), whose last check re-reads the bytes;
  on `target_changed` this is tried once more.

Only the task's line changes, through `horae_obsidian.writer.put` (atomic, no symbolic link,
no hard link, case and Unicode twins refused): `done` is `Task.tick` (`✅` dated today in
the instance time zone, the `TZ` the core gives the plugin), `cancelled` `Task.cancel`
(`❌`), `todo` `Task.reopen` (both dates removed; a task already open, `[/]` or a theme's
status included, is left as it is), `snoozed` `Task.snooze`: the core's `until` becomes
Horae's reminder, `⏰ YYYY-MM-DD` for a date, `⏰ YYYY-MM-DD HH:MM` for a date-time (in the
instance time zone, by its rules: `clock.local`, rounded up to the minute), before the Tasks
date fields. A task already in the asked state is left as it is (`changed: false`), and so
is a recurring task asked `done` or `cancelled` (`details.recurring`: Horae never ends a
recurrence, tick it in Obsidian). `Applied.details` records the note, the line and both line
digests (not the line itself: the core does not undo task changes yet); `restore_status`
puts a line back given it.

**`task.create`** (`create`). The line is `horae_sdk.tasks.task_line` (title escaped, link
inert) with Horae's order: `⏰` before `📅`, and `^h-<hash>` last (`render_line`: horae-sdk
0.3's `task_line(..., fingerprint=)`). It is appended to the entry's `note` parameter (trusted;
default `Tasks.assistant.md`), which must be a note Horae owns (`*.assistant.md`,
`not_owned`), created when missing, under the index lock: the line is written, then the
index. It is not written again when the fingerprint's task is in the vault, edited, moved or
copied (`changed: false`); when the writer's note holds one line with its block id that the
index does not know (the index was lost, or a crash came between the two writes), that line
is **adopted** (`adopted: true`; several: `task_unadoptable`); when the user deleted it
(a tombstone), it is `dismissed` for `recreate_after_days` (default 30).
"""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from horae_sdk import Applied, Paths, SinkError
from horae_sdk import tasks as sdk_tasks

from horae_obsidian import clock as instance
from horae_obsidian import writer
from horae_obsidian.index import Entries, Link, TaskIndex, block_of
from horae_obsidian.provenance import DEFAULT_NOTE, Provenance, View, same_note
from horae_obsidian.tasks import (
    ENCODING_ERRORS,
    HORAE_BLOCK,
    NAME,
    Document,
    RecurringTaskError,
    Task,
    json_safe,
    parse,
)
from horae_obsidian.vault import Scan, VaultError, safe_path, scan

TASK_TYPES = ("task.create", "task.update_status")
STATUSES = ("todo", "done", "cancelled", "snoozed")
RECREATE_AFTER_DAYS = 30
"""How long a task the user deleted is not created again (`recreate_after_days`)."""
MAX_RECREATE_AFTER_DAYS = 3650
Clock = Callable[[], datetime]
now = instance.now
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HORAE_KEY = f"{NAME}:horae:"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", ENCODING_ERRORS)).hexdigest()


def _vault(paths: Paths) -> str:
    vault = writer.vault_of(paths)
    if paths.allowed(vault, write=True) is None:
        raise SinkError("outside_vault", "the vault is not an approved paths.write entry")
    return vault


def _scan(vault: str) -> Scan:
    try:
        return scan(vault)
    except VaultError as error:
        raise SinkError("unreadable", str(error)) from None


def _entries(vault: str) -> Entries:
    """The index; empty when it cannot be read (its links are then unknown: fail closed)."""
    try:
        return TaskIndex(vault).read()
    except SinkError:
        return Entries()


def refuse_tasks_note(paths: Paths, path: object) -> None:
    """`tasks_note` when `path` is a Tasks writer note (`provenance`): writer-exclusive, no
    note intent writes or undoes it. `index_unavailable` when the index cannot be read."""
    if not isinstance(path, str) or not writer.is_owned(path):
        return
    if Provenance(TaskIndex(writer.vault_of(paths)).read()).writer_note(path):
        raise SinkError(
            "tasks_note",
            f"{safe_path(path)} is the Tasks writer's note: only task intents change it",
        )


# --- task.update_status -------------------------------------------------------------------


def _target_block(target: str) -> str | None:
    """The block id a target names: a fingerprint's, or an `obsidian:horae:` key's."""
    if target.startswith(_HORAE_KEY):
        block = target[len(_HORAE_KEY) :]
        return block if HORAE_BLOCK.fullmatch(block) else None
    if target.startswith(f"{NAME}:"):
        return None
    try:
        return block_of(target)
    except ValueError:
        return None


def _matcher(view: View, target: str) -> Callable[[Task], bool]:
    """Whether a task is the one `target` names (before uniqueness is checked)."""
    block = _target_block(target)
    hidden = view.provenance.hidden
    if target.startswith(f"{NAME}:"):
        return lambda task: task.key == target and hidden(task) is None
    if block is None:
        return lambda task: False
    return lambda task: view.carries(task, block)


def locate(view: View, target: str) -> tuple[Document, Task]:
    """The one task `target` names (its key, or the fingerprint behind its `^h-` block id),
    and its note; `SinkError` unless it is found once and shown unique. Horae's output
    (`Provenance.hidden`) is never a target, as the source never lists it."""
    match = _matcher(view, target)
    matches = [(document, task) for document, task in view.tasks() if match(task)]
    shown = safe_path(target)
    if not matches:
        raise SinkError("task_not_found", f"no task {shown} in the vault")
    if len(matches) > 1:
        raise SinkError("task_ambiguous", f"{len(matches)} tasks match {shown}: none is changed")
    document, task = matches[0]
    by_block = not target.startswith(f"{NAME}:") and view.provenance.link(task) is not None
    if not by_block and not view.known_unique(task):
        why = (
            "another task has the same key"
            if view.counts[task.key] != 1
            else "the scan did not read every note, so its key may not be unique"
        )
        raise SinkError("task_ambiguous", f"{shown}: {why}; nothing is changed")
    return document, task


def _quick(paths: Paths, view: View, target: str) -> tuple[Document, Task] | None:
    """The task in its writer note, read alone (no scan), or `None` (not there: scan)."""
    block = _target_block(target)
    link = view.provenance.links.get(block or "")
    if block is None or link is None or link.path is None:
        return None
    try:
        data = writer.read(paths, link.path)
    except SinkError:
        return None
    if data is None:
        return None
    document = parse(data, link.path)
    mine = [
        task
        for task in document.tasks
        if task.horae_block == block
        and view.provenance.link(task) is not None
        and (not target.startswith(_HORAE_KEY) or task.key == target)
    ]
    if len(mine) > 1:
        raise SinkError("task_ambiguous", f"{len(mine)} lines hold {safe_path(target)}")
    return (document, mine[0]) if mine else None


def _fresh(paths: Paths, view: View, target: str, task: Task) -> tuple[Document, Task]:
    """The task's note read again now: the task found again by key and line digest."""
    data = writer.read(paths, task.path)
    shown = safe_path(task.path)
    if data is None:
        raise SinkError("target_changed", f"{shown} no longer exists")
    document = view.rekey(parse(data, task.path))
    match = _matcher(view, target)
    again = [t for t in document.tasks if match(t) and t.line_sha256 == task.line_sha256]
    if not again:
        raise SinkError("target_changed", f"{shown}: the task line changed")
    if len(again) > 1:
        raise SinkError("task_ambiguous", f"{shown}: the task line is there twice now")
    return document, again[0]


def reminder_for(until: object) -> str:
    """The `⏰` value for a snooze `until`: the date, or the date-time in the instance time
    zone (by its rules at that moment, `clock.local`), rounded up to the minute."""
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
            extra = moment.second + moment.microsecond / 1e6
            moment = moment.replace(second=0, microsecond=0)
            if extra:
                moment += timedelta(minutes=math.ceil(extra / 60))
            return instance.local(moment).strftime("%Y-%m-%d %H:%M")
    raise SinkError("intent_invalid", "snoozed needs until: a date or an RFC 3339 date-time")


def new_line(task: Task, status: str, until: object, clock: Clock = now) -> str:
    """`task`'s line in `status` (see the module docstring). `RecurringTaskError` when a
    recurring task is asked to close."""
    today = clock().date()
    try:
        if status == "done":
            return task.line.text if task.status == "done" else task.tick(today)
        if status == "cancelled":
            return task.line.text if task.status == "cancelled" else task.cancel(today)
    except RecurringTaskError:
        raise
    except ValueError as error:
        raise SinkError("task_ambiguous", str(error)) from None
    if status == "todo":
        return task.line.text if task.is_open else task.reopen()
    if status == "snoozed":
        value = reminder_for(until)
        if not task.is_open:
            raise SinkError(
                "task_closed", f"the task is {task.status}: reopen it (todo) before snoozing it"
            )
        reminder = task.reminder
        current = None
        if reminder is not None:
            current = " ".join(p for p in (str(reminder.date or ""), reminder.time or "") if p)
        if current == value and len(_reminders(task)) == 1:
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
    vault = _vault(paths)
    provenance = Provenance(_entries(vault))
    view = View(Scan((), (), False), provenance, frozenset(), Counter())  # no scan yet
    located = _quick(paths, view, target)
    if located is None:
        view = View.of(_scan(vault), provenance)
        located = locate(view, target)
    for attempt in (1, 2):
        try:
            return _write_status(paths, view, target, located[1], intent, clock)
        except SinkError as error:
            if error.kind != "target_changed" or attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def _write_status(
    paths: Paths, view: View, target: str, located: Task, intent: dict[str, Any], clock: Clock
) -> Applied:
    """One attempt: the note read again, the line changed with a compare-and-swap."""
    document, task = _fresh(paths, view, target, located)
    status = str(intent["status"])
    details: dict[str, Any] = {
        "path": json_safe(task.path),
        "line": task.line.number,
        "status": status,
        "before_line_sha256": task.line_sha256,
        "before_sha256": document.sha256,
    }
    fingerprint = view.source_fingerprint(task)
    if fingerprint is not None:
        details["source_fingerprint"] = fingerprint
    try:
        text = new_line(task, status, intent.get("until"), clock)
    except RecurringTaskError:
        return Applied(
            target, details={**details, "changed": False, "recurring": "tick in Obsidian"}
        )
    if text == task.line.text:
        return Applied(target, details={**details, "changed": False})
    after = document.replace_line(task, text)
    writer.put(paths, task.path, after, document.sha256, _check_line(document, task, after))
    details.update(
        changed=True,
        after_line_sha256=_sha(text),
        after_sha256=hashlib.sha256(after).hexdigest(),
    )
    if not _relink(paths, view.provenance, task, _sha(text)):
        details["index"] = "stale"
    return Applied(target, details=details)


def _relink(paths: Paths, provenance: Provenance, task: Task, line_sha256: str) -> bool:
    """After the sink changed Horae's task line in its writer note, record its new digest
    (whatever the recorded one was: a stale entry heals on the next change). Tried twice;
    False when the index could not be updated (the line is then reported as edited)."""
    link = provenance.link(task)
    block = task.horae_block
    if link is None or block is None:
        return True
    index = TaskIndex(writer.vault_of(paths))
    for _ in range(2):
        try:
            with index.locked() as entries:
                current = entries.links.get(block)
                if current is None or current.path is None:
                    return True
                if current.fingerprint != link.fingerprint or not same_note(
                    current.path, task.path
                ):
                    return True
                entries.links[block] = Link(
                    current.fingerprint, current.at, current.path, line_sha256
                )
                index.save(entries)
            return True
        except (SinkError, OSError):
            continue
    return False


def restore_status(paths: Paths, details: dict[str, Any]) -> bool:
    """Undo a status change: put back `details["before_line"]` (the line `update_status`
    replaced; its details do not carry it: the core's undo will supply it from its own
    journal) if the note still holds the changed line exactly once (`target_changed`
    otherwise). Returns whether it wrote (false: nothing was changed, or already restored)."""
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
    _relink(paths, Provenance(_entries(writer.vault_of(paths))), task, _sha(before_line))
    return True


# --- task.create -------------------------------------------------------------------------


def render_line(intent: dict[str, Any]) -> str:
    """The Tasks line for a `task.create` intent: horae-sdk's `task_line(title, due=,
    time=, link=, fingerprint=)`: title escaped, link inert (`link_destination`; left out over
    `MAX_DESTINATION`), `⏰ HH:MM` before `📅 YYYY-MM-DD`, and `^h-<hash>` last."""
    title, due, time, link = (intent.get(k) for k in ("title", "due", "time", "link"))
    fingerprint = intent.get("fingerprint")
    if not isinstance(title, str) or not isinstance(fingerprint, str):
        raise SinkError("intent_invalid", "task.create needs a title and a fingerprint")
    if not ((due is None or isinstance(due, str)) and (link is None or isinstance(link, str))):
        raise SinkError("intent_invalid", "due and link must be strings")
    if time is not None and not isinstance(time, str):
        raise SinkError("intent_invalid", "time must be a string")
    try:
        return sdk_tasks.task_line(title, due=due, time=time, link=link, fingerprint=fingerprint)
    except ValueError as error:
        raise SinkError("intent_invalid", str(error)) from None


def _recreate_after(params: dict[str, Any]) -> timedelta:
    days = params.get("recreate_after_days", RECREATE_AFTER_DAYS)
    if (
        isinstance(days, bool)
        or not isinstance(days, int)
        or not 0 <= days <= MAX_RECREATE_AFTER_DAYS
    ):
        raise SinkError(
            "config_invalid",
            f"recreate_after_days must be a whole number of days, 0 to {MAX_RECREATE_AFTER_DAYS}",
        )
    return timedelta(days=days)


def create(
    paths: Paths, params: dict[str, Any], intent: dict[str, Any], clock: Clock = now
) -> Applied:
    """Apply a `task.create` intent (see the module docstring)."""
    note = params.get("note", DEFAULT_NOTE)
    writer.note_parts(note)
    if not isinstance(note, str) or not writer.is_owned(note):
        raise SinkError("not_owned", f"tasks are created in *{writer.OWNED_SUFFIX} notes only")
    window = _recreate_after(params)
    line = render_line(intent)
    fingerprint = str(intent["fingerprint"])
    block = block_of(fingerprint)
    vault = _vault(paths)
    started = clock()
    found = _scan(vault)
    details: dict[str, Any] = {"block_id": block, "dedupe_complete": found.keys_complete}
    if not found.keys_complete:
        details["dedupe_warning"] = (
            "the scan did not read every note (see obsidian.open_tasks skipped): a copy of"
            " this task in a note it did not read is not seen, so it may be written twice"
        )
    index = TaskIndex(vault)
    with index.locked() as entries:
        provenance = Provenance(entries)
        view = View.of(found, provenance)
        before = writer.read(paths, note)  # under the lock: a concurrent create is seen
        mine = parse(before or b"", note)
        lines = [task for _, task in view.tasks()] + list(mine.tasks)
        present = {t.horae_block for t in lines if t.horae_block and not t.in_owned_block}
        existing = [
            task for task in lines if task.horae_block == block and provenance.hidden(task) is None
        ]
        if existing:
            task = existing[0]
            return Applied(
                fingerprint,
                details={
                    **details,
                    "changed": False,
                    "path": json_safe(task.path),
                    "line": task.line.number,
                },
            )
        orphans = [
            task
            for task in mine.tasks
            if task.horae_block == block
            and not task.in_owned_block
            and provenance.link(task) is None
        ]
        if len(orphans) > 1:
            raise SinkError(
                "task_unadoptable",
                f"{safe_path(note)} holds {len(orphans)} lines with this task's block id and"
                " the index records none: keep one, then the task is adopted",
            )
        if found.keys_complete:
            index.retire(entries, present, started)
        if orphans:
            index.add(entries, fingerprint, clock(), path=note, line_sha256=orphans[0].line_sha256)
            index.save(entries, present)
            return Applied(
                fingerprint,
                details={
                    **details,
                    "changed": False,
                    "adopted": True,
                    "path": note,
                    "line": orphans[0].line.number,
                },
            )
        tomb = entries.tombstones.get(block)
        if tomb is not None and tomb.fingerprint == fingerprint and clock() - tomb.at < window:
            index.save(entries, present)
            return Applied(
                fingerprint,
                details={
                    **details,
                    "changed": False,
                    "dismissed": True,
                    "dismissed_at": tomb.at.isoformat(),
                    "recreate_after_days": window.days,
                },
            )
        index.add(entries, fingerprint, clock(), path=note, line_sha256=_sha(line))
        number = _append(paths, note, before, line, block)
        index.save(entries, present | {block})
    return Applied(fingerprint, details={**details, "changed": True, "path": note, "line": number})


def _append(paths: Paths, note: str, before: bytes | None, line: str, block: str) -> int:
    """Appends `line` to `note` (whose bytes are `before`); its line number."""
    eol = b"\r\n" if before is not None and b"\r\n" in before else b"\n"
    head = before or b""
    if head and not head.endswith((b"\n", b"\r")):
        head += eol
    after = head + line.encode("utf-8") + eol

    def check(current: bytes | None) -> None:
        if current != before:
            raise SinkError("target_changed", f"{safe_path(note)} changed while it was read")

    end = len(after) - len(eol)
    written = [t for t in parse(after, note).tasks if t.horae_block == block and t.line.end == end]
    if len(written) != 1:
        raise SinkError(
            "note_invalid",
            f"{safe_path(note)}: a line appended there would not read as a task (an unclosed"
            " code fence or comment?)",
        )
    writer.put(paths, note, after, writer.sha256(before), check, creating=before is None)
    return written[0].line.number


__all__ = [
    "DEFAULT_NOTE",
    "RECREATE_AFTER_DAYS",
    "TASK_TYPES",
    "create",
    "locate",
    "new_line",
    "refuse_tasks_note",
    "reminder_for",
    "render_line",
    "restore_status",
    "update_status",
]
