# SPDX-License-Identifier: Apache-2.0
"""#4: `task.update_status` edits exactly one line, finds its task by key or by the `^h-` link
back, fails closed (ambiguous, stale, recurring), snoozes and undoes; `task.create` writes
the line and the index."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError
from horae_sdk.testing import operation

import horae_obsidian.tasksink as tasksink
from horae_obsidian import build
from horae_obsidian.index import MAX_ENTRIES, TaskIndex, block_of
from horae_obsidian.sink import TASK_TOOL, TOOL, VaultSink
from horae_obsidian.source import open_tasks
from horae_obsidian.tasks import parse
from horae_obsidian.vault import scan

PARIS = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 8, 23, 30, tzinfo=PARIS)  # 22:30 UTC: still the 8th in the vault's TZ
NOTE = (
    "﻿# Week\r\n"
    "- [ ] Pay rent 📅 2026-11-10 #home\r\n"
    "- [ ] Call \udcff Ada 🆔 ada\r\n"  # invalid UTF-8 elsewhere in the note is kept
    "- [ ] Water 🔁 every week 📅 2026-11-09\r\n"
    "  - [/] Draft ⏰ 2026-11-09 08:00\r\n"
    "no newline at the end"
).encode("utf-8", "surrogateescape")


def clock() -> datetime:
    return NOW


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Notes").mkdir(parents=True)
    (root / "Notes" / "week.md").write_bytes(NOTE)
    return root


def wire(vault: Path) -> Paths:
    return Paths(write=(str(vault),))


def apply(vault: Path, intent: dict[str, Any], **params: Any) -> Applied:
    request = ApplyRequest(wire(vault), intent, tool=TASK_TOOL, params=params)
    return asyncio.run(VaultSink([TOOL, TASK_TOOL], clock).apply(request))


def status(task: str, value: str, until: str | None = None) -> dict[str, Any]:
    intent = {"type": "task.update_status", "task": task, "status": value}
    return intent if until is None else {**intent, "until": until}


def refused(vault: Path, intent: dict[str, Any]) -> str:
    with pytest.raises(SinkError) as raised:
        apply(vault, intent)
    return raised.value.kind


def key_of(vault: Path, description: str) -> str:
    found = scan(vault)
    return next(t.key for d in found.documents for t in d.tasks if t.description == description)


def changed_lines(before: bytes, after: bytes) -> list[tuple[bytes, bytes]]:
    old, new = before.split(b"\n"), after.split(b"\n")
    assert len(old) == len(new)
    return [(a, b) for a, b in zip(old, new, strict=True) if a != b]


# --- one line, exactly ---------------------------------------------------------------------


def test_ticking_edits_exactly_one_line(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    applied = apply(vault, status(key, "done"))
    after = (vault / "Notes" / "week.md").read_bytes()
    assert changed_lines(NOTE, after) == [
        (
            "- [ ] Pay rent 📅 2026-11-10 #home\r".encode(),
            "- [x] Pay rent 📅 2026-11-10 ✅ 2026-11-08 #home\r".encode(),
        )
    ]
    assert after.startswith(b"\xef\xbb\xbf")  # the BOM is kept
    assert b"\xff" in after  # and so are the bytes that are not UTF-8
    assert applied.target == key
    assert applied.details["changed"] is True
    assert applied.details["line"] == 2
    assert applied.details["after_sha256"] == hashlib.sha256(after).hexdigest()
    json.dumps(applied.details, allow_nan=False)
    # the key did not move: done again is a no-op, and the source no longer lists it
    again = apply(vault, status(key, "done"))
    assert again.details["changed"] is False
    assert (vault / "Notes" / "week.md").read_bytes() == after
    assert "Pay rent #home" not in [t["description"] for t in open_tasks(wire(vault))["tasks"]]


def test_done_is_dated_in_the_instance_time_zone(vault: Path) -> None:
    def late() -> datetime:
        return datetime(2026, 11, 8, 23, 30, tzinfo=UTC).astimezone(PARIS)  # the 9th in Paris

    key = key_of(vault, "Pay rent #home")
    request = ApplyRequest(wire(vault), status(key, "done"), tool=TASK_TOOL)
    asyncio.run(VaultSink([TASK_TOOL], late).apply(request))
    assert "✅ 2026-11-09".encode() in (vault / "Notes" / "week.md").read_bytes()


def test_cancel_and_todo_undo_and_restore(vault: Path) -> None:
    note = vault / "Notes" / "week.md"
    key = key_of(vault, "Pay rent #home")
    done = apply(vault, status(key, "done"))
    assert tasksink.restore_status(wire(vault), done.details) is True
    assert note.read_bytes() == NOTE  # the undo puts the exact line back
    assert tasksink.restore_status(wire(vault), done.details) is False  # already restored
    cancelled = apply(vault, status(key, "cancelled"))
    assert "- [-] Pay rent 📅 2026-11-10 ❌ 2026-11-08 #home".encode() in note.read_bytes()
    # an undo over a line edited since is refused
    note.write_bytes(note.read_bytes().replace("❌ 2026-11-08".encode(), "❌ 2026-11-07".encode()))
    with pytest.raises(SinkError) as raised:
        tasksink.restore_status(wire(vault), cancelled.details)
    assert raised.value.kind == "target_changed"
    reopened = apply(vault, status(key, "todo"))
    assert reopened.details["changed"] is True
    assert note.read_bytes() == NOTE
    assert apply(vault, status(key, "todo")).details["changed"] is False
    with pytest.raises(SinkError, match="record"):
        tasksink.restore_status(wire(vault), {"changed": True, "path": "Notes/week.md"})
    assert tasksink.restore_status(wire(vault), {"changed": False}) is False


def test_snooze_writes_the_reminder_before_the_dates(vault: Path) -> None:
    note = vault / "Notes" / "week.md"
    key = key_of(vault, "Pay rent #home")
    apply(vault, status(key, "snoozed", "2026-11-09T07:00:30Z"))  # 08:00:30 in Paris
    assert "- [ ] Pay rent ⏰ 2026-11-09 08:01 📅 2026-11-10 #home\r".encode() in note.read_bytes()
    apply(vault, status(key, "snoozed", "2026-11-12"))
    assert "- [ ] Pay rent ⏰ 2026-11-12 📅 2026-11-10 #home\r".encode() in note.read_bytes()
    assert apply(vault, status(key, "snoozed", "2026-11-12")).details["changed"] is False
    # a recurring task may be snoozed, never closed
    water = key_of(vault, "Water")
    assert refused(vault, status(water, "done")) == "recurring_task"
    assert refused(vault, status(water, "cancelled")) == "recurring_task"
    apply(vault, status(water, "snoozed", "2026-11-10"))
    assert "- [ ] Water ⏰ 2026-11-10 🔁 every week".encode() in note.read_bytes()
    for until in (None, "soon", "2026-11-09T08:00:00", "2026-02-30"):
        intent = status(key, "snoozed")
        if until is not None:
            intent["until"] = until
        assert refused(vault, intent) == "intent_invalid"


# --- fail closed ------------------------------------------------------------------------------


def test_unknown_and_ambiguous_tasks_are_refused(vault: Path) -> None:
    assert refused(vault, status("obsidian:id:nope", "done")) == "task_not_found"
    assert refused(vault, status("github:pr:404", "done")) == "task_not_found"
    assert refused(vault, {"type": "task.update_status", "task": 1, "status": "x"}) == (
        "intent_invalid"
    )
    (vault / "Notes" / "copy.md").write_text("- [ ] Call Ada again 🆔 ada\n")
    assert refused(vault, status("obsidian:id:ada", "done")) == "task_ambiguous"
    (vault / "Notes" / "copy.md").unlink()
    apply(vault, status("obsidian:id:ada", "done"))  # unique again
    # a note the scan cannot read: a vault-scoped key can no longer be shown unique
    (vault / "Notes" / "linked.md").symlink_to(vault / "Notes" / "week.md")
    assert refused(vault, status("obsidian:id:ada", "todo")) == "task_ambiguous"
    # a note-scoped key stays usable: its note was read whole
    apply(vault, status(key_of(vault, "Pay rent #home"), "done"))


def test_a_note_changed_after_the_scan_is_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = vault / "Notes" / "week.md"
    key = key_of(vault, "Pay rent #home")

    def racing(path: str) -> Any:
        found = scan(path)
        note.write_bytes(NOTE + b"\nedited meanwhile")  # the user saves the note
        return found

    monkeypatch.setattr(tasksink, "scan", racing)
    assert refused(vault, status(key, "done")) == "target_changed"
    assert note.read_bytes() == NOTE + b"\nedited meanwhile"  # nothing written


def test_the_line_is_checked_again_before_the_rename() -> None:
    document = parse(NOTE, "Notes/week.md")
    task = document.tasks[0]
    after = document.replace_line(task, task.tick(NOW.date()))
    check = tasksink._check_line(document, task, after)
    check(NOTE)
    for before, why in (
        (NOTE.replace(b"rent", b"Rent"), "line changed"),
        (b"\xef\xbb\xbf# Week\r\n- [ ] other\r\n", "line changed"),
        (NOTE.replace(b"Ada", b"Bob"), "since the scan"),
        (None, "no longer exists"),
    ):
        with pytest.raises(SinkError, match=why) as raised:
            check(before)
        assert raised.value.kind == "target_changed"
    other = document.replace_line(document.tasks[1], "- [x] changed")
    with pytest.raises(SinkError) as raised:
        tasksink._check_line(document, task, other)(NOTE)
    assert raised.value.kind == "not_owned"


# --- task.create and the link back ----------------------------------------------------------


def test_horae_output_is_never_a_target(vault: Path) -> None:
    (vault / "Brief.assistant.md").write_text("- [ ] Written by the LLM\n")
    (vault / "Notes" / "week.md").write_bytes(
        NOTE + b"\r\n<!-- horae:begin b -->\r\n- [ ] In a block\r\n<!-- horae:end b -->\r\n"
    )
    for description in ("Written by the LLM", "In a block"):
        assert refused(vault, status(key_of(vault, description), "done")) == "task_not_found"


def test_create_then_update_by_fingerprint_round_trip(vault: Path) -> None:
    intent = {
        "type": "task.create",
        "title": "Review PR 42 📅 2020-01-01 ^h-000000000000",
        "fingerprint": "github:pr:42",
        "due": "2026-11-12",
        "time": "09:30",
    }
    created = apply(vault, intent)
    block = block_of("github:pr:42")
    owned = vault / "Tasks.assistant.md"
    line = owned.read_text("utf-8").splitlines()[0]
    assert line.endswith(f" ⏰ 09:30 📅 2026-11-12 ^{block}")
    assert created.target == "github:pr:42"
    assert created.details == {
        "block_id": block,
        "dedupe_complete": True,
        "changed": True,
        "path": "Tasks.assistant.md",
        "line": 1,
        "line_text": line,
    }
    # the same fingerprint again: deduplicated across runs
    assert apply(vault, intent).details["changed"] is False
    # the source reports the fingerprint; the user edits the text, the link survives
    [task] = [t for t in open_tasks(wire(vault))["tasks"] if t["source_fingerprint"]]
    assert task["key"] == f"obsidian:horae:{block}"
    owned.write_text(owned.read_text("utf-8").replace("Review", "Re-review"), "utf-8")
    (vault / "Notes" / "moved.md").write_text(owned.read_text("utf-8"), "utf-8")
    owned.write_text("", "utf-8")  # moved to another note
    done = apply(vault, status("github:pr:42", "done"))
    assert done.details["source_fingerprint"] == "github:pr:42"
    assert done.details["path"] == "Notes/moved.md"
    assert b"- [x] Re-review" in (vault / "Notes" / "moved.md").read_bytes()
    # by its key too
    assert apply(vault, status(task["key"], "todo")).details["changed"] is True
    # copied into a second note: both carry the block id, so neither is changed
    (vault / "Notes" / "twin.md").write_bytes((vault / "Notes" / "moved.md").read_bytes())
    assert refused(vault, status("github:pr:42", "done")) == "task_ambiguous"


def test_create_appends_to_an_owned_note_only(vault: Path) -> None:
    owned = vault / "Inbox.assistant.md"
    owned.write_bytes(b"# Inbox\r\n- [ ] old")
    intent = {"type": "task.create", "title": "New", "fingerprint": "mail:7"}
    applied = apply(vault, intent, note="Inbox.assistant.md")
    assert owned.read_bytes() == (
        f"# Inbox\r\n- [ ] old\r\n- [ ] New ^{block_of('mail:7')}\r\n".encode()
    )
    assert applied.details["line"] == 3
    with pytest.raises(SinkError) as raised:
        apply(vault, {**intent, "fingerprint": "mail:8"}, note="Notes/week.md")
    assert raised.value.kind == "not_owned"
    (vault / "Fence.assistant.md").write_text("```\n")
    with pytest.raises(SinkError) as raised:
        apply(vault, {**intent, "fingerprint": "mail:9"}, note="Fence.assistant.md")
    assert raised.value.kind == "note_invalid"
    for bad in (
        {"type": "task.create", "title": "x"},
        {**intent, "due": "2026-02-30"},
        {**intent, "time": "09:00"},
        {**intent, "due": 1},
        {**intent, "time": 9},
    ):
        with pytest.raises(SinkError) as raised:
            apply(vault, bad)
        assert raised.value.kind == "intent_invalid"
    with pytest.raises(SinkError) as raised:
        asyncio.run(VaultSink([TASK_TOOL]).apply(ApplyRequest(wire(vault), intent, content=b"x")))
    assert raised.value.kind == "intent_invalid"


def test_a_link_is_rendered_inert_by_the_sdk(vault: Path) -> None:
    intent = {
        "type": "task.create",
        "title": "Review",
        "fingerprint": "github:pr:9",
        "link": "https://code.example.org/pr/9(x)",
    }
    # the entry's link_prefix (core routing) is accepted and ignored
    apply(vault, intent, mode="tasks", link_prefix="https://code.example.org/")
    line = (vault / "Tasks.assistant.md").read_text("utf-8").splitlines()[0]
    assert "[link](https://code.example.org/pr/9%28x%29)" in line
    [task] = open_tasks(wire(vault))["tasks"][-1:]
    assert task["link"] == "https://code.example.org/pr/9%28x%29"
    assert task["source_fingerprint"] == "github:pr:9"
    with pytest.raises(SinkError) as raised:
        apply(vault, {**intent, "fingerprint": "x:1", "link": "http://insecure.example/"})
    assert raised.value.kind == "intent_invalid"


# --- the index ---------------------------------------------------------------------------------


def test_the_index_is_atomic_bounded_and_fails_closed(vault: Path) -> None:
    index = TaskIndex(str(vault), max_entries=2)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with index.locked() as entries:
        for n in range(3):
            index.add(entries, f"mail:{n}", start + timedelta(minutes=n))
        index.save(entries)
    assert set(index.read().fingerprints().values()) == {"mail:1", "mail:2"}
    assert MAX_ENTRIES >= 10_000
    directory = vault / ".horae"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in directory.iterdir() if p.name.endswith(".tmp")] == []
    # a collision of two fingerprints on one block id is refused
    with index.locked() as entries:
        block = block_of("mail:1")
        entries.links[block] = ("mail:other", start)
        with pytest.raises(SinkError) as raised:
            index.add(entries, "mail:1", start)
        assert raised.value.kind == "block_collision"
    # corrupt content starts over; another format or a link is refused
    path = directory / "obsidian-tasks.json"
    path.write_text("{not json")
    assert index.read().reset
    path.write_text(json.dumps({"format": 99, "tasks": {}}))
    with pytest.raises(SinkError, match="format"):
        index.read()
    path.unlink()
    path.symlink_to(vault / "Notes" / "week.md")
    with pytest.raises(SinkError) as raised:
        index.read()
    assert raised.value.kind == "index_unavailable"
    path.unlink()
    os.chmod(directory, 0o777)  # noqa: S103 (the permissive mode under test)
    with pytest.raises(SinkError, match="chmod"), index.locked():
        pass
    os.chmod(directory, 0o700)
    # the source never fails over the index: it reports it
    path.write_text(json.dumps({"format": 99}))
    assert open_tasks(wire(vault))["index"] == "unavailable (index_unavailable)"


def test_the_index_lock_times_out(vault: Path) -> None:
    index = TaskIndex(str(vault), lock_timeout_s=0.1)
    with (
        index.locked(),
        pytest.raises(SinkError) as raised,
        TaskIndex(str(vault), lock_timeout_s=0.1).locked(),
    ):
        pass
    assert raised.value.kind == "index_busy"


# --- over MCP -------------------------------------------------------------------------------------


def test_task_intents_through_the_sink_operations(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    paths = {"read": [], "write": [str(vault)]}

    async def scenario() -> dict[str, Any]:
        return await operation(
            build(),
            "obsidian.apply",
            {"paths": paths, "intent": status(key, "done"), "tool": TASK_TOOL, "params": {}},
        )

    result = anyio.run(scenario)
    assert result["target"] == key
    assert result["details"]["changed"] is True
    assert "✅".encode() in (vault / "Notes" / "week.md").read_bytes()
