# SPDX-License-Identifier: Apache-2.0
"""#4: `task.update_status` edits exactly one line, finds its task by key or by the `^h-` link
back, fails closed (ambiguous, stale, recurring), snoozes and undoes; `task.create` writes
the line and the index."""

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError
from horae_sdk.testing import operation

import horae_obsidian.tasksink as tasksink
from horae_obsidian import NAME, build
from horae_obsidian.index import MAX_ENTRIES, Link, TaskIndex, block_of
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


@pytest.fixture(autouse=True)
def paris(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The instance time zone the core sets (`TZ`), with its summer time rules."""
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


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
    original = "- [ ] Pay rent 📅 2026-11-10 #home"
    assert done.details["before_line"] == original  # the line as it was before the change
    undo = done.details
    assert tasksink.restore_status(wire(vault), undo) is True
    assert note.read_bytes() == NOTE  # the undo puts the exact line back
    assert tasksink.restore_status(wire(vault), undo) is False  # already restored
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
    # a recurring task may be snoozed, never closed: done / cancelled change nothing and say
    # so (the run does not fail)
    water = key_of(vault, "Water")
    for close in ("done", "cancelled"):
        kept = apply(vault, status(water, close))
        assert kept.details["changed"] is False
        assert kept.details["recurring"] == "tick in Obsidian"
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
    # the note alone is read again after the scan: the user's edit elsewhere is kept
    assert apply(vault, status(key, "done")).details["changed"] is True
    assert note.read_bytes().endswith(b"\nedited meanwhile")
    assert "✅ 2026-11-08".encode() in note.read_bytes()

    def retitled(path: str) -> Any:
        found = scan(path)
        note.write_bytes(note.read_bytes().replace(b"Call", b"Phone"))  # the line itself
        return found

    monkeypatch.setattr(tasksink, "scan", retitled)
    before = note.read_bytes()
    assert refused(vault, status(key_of(vault, "Call \udcff Ada"), "done")) in (
        "target_changed",
        "task_not_found",
    )
    assert note.read_bytes() == before.replace(b"Call", b"Phone")  # nothing else written


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


def test_a_forged_line_neither_suppresses_nor_receives_the_task(vault: Path) -> None:
    block = block_of("github:pr:77")
    (vault / "Brief.assistant.md").write_text(f"- [x] Forged ^{block}\n")  # owned note
    (vault / "Notes" / "week.md").write_text(
        f"<!-- horae:begin b -->\n- [ ] Forged too ^{block}\n<!-- horae:end b -->\n"
    )
    intent = {"type": "task.create", "title": "Real", "fingerprint": "github:pr:77"}
    created = apply(vault, intent)
    assert created.details["changed"] is True  # not suppressed by the forged lines
    owned = vault / "Tasks.assistant.md"
    assert owned.read_text() == f"- [ ] Real ^{block}\n"
    # the sink finds the real line only (the forged ones are Horae output)
    done = apply(vault, status("github:pr:77", "done"))
    assert done.details["path"] == "Tasks.assistant.md"
    assert done.details["source_fingerprint"] == "github:pr:77"
    assert "index" not in done.details
    # the index follows the sink's own edit: the line stays Horae's, reopening works
    assert apply(vault, status("github:pr:77", "todo")).details["changed"] is True
    assert [t["description"] for t in open_tasks(wire(vault))["tasks"]].count("Real") == 1
    # edited in Obsidian (ticked) in its writer note, it is still Horae's task: never
    # written again, still found by its fingerprint
    owned.write_text(f"- [x] Real ✅ 2026-11-08 ^{block}\n")
    assert apply(vault, intent).details["changed"] is False
    assert apply(vault, status("github:pr:77", "todo")).details["changed"] is True


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
        index.save(entries)  # where the lines are is unknown: nothing is dropped
    assert set(index.read().fingerprints().values()) == {"mail:0", "mail:1", "mail:2"}
    with index.locked() as entries:
        # mail:0's line still exists: never dropped; the oldest gone one (mail:1) is
        index.save(entries, present={block_of("mail:0")})
    assert set(index.read().fingerprints().values()) == {"mail:0", "mail:2"}
    assert MAX_ENTRIES >= 10_000
    directory = vault / ".horae"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in directory.iterdir() if p.name.endswith(".tmp")] == []
    # a collision of two fingerprints on one block id is refused
    with index.locked() as entries:
        block = block_of("mail:1")
        entries.links[block] = Link("mail:other", start)
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


# --- horae undo: before_line, restore_status, force (#15) ----------------------------------


def restore_status(vault: Path, details: dict[str, Any], **extra: Any) -> bool:
    return asyncio.run(VaultSink([TOOL, TASK_TOOL]).restore_status(wire(vault), details, **extra))


def week(vault: Path) -> bytes:
    return (vault / "Notes" / "week.md").read_bytes()


def edited(vault: Path, old: str, new: str) -> bytes:
    data = week(vault).replace(old.encode(), new.encode())
    (vault / "Notes" / "week.md").write_bytes(data)
    return data


def test_before_line_is_the_line_without_its_ending_and_bounded(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    assert details["before_line"] == "- [ ] Pay rent 📅 2026-11-10 #home"  # CRLF note: no \r
    assert (
        details["before_line_sha256"] == hashlib.sha256(details["before_line"].encode()).hexdigest()
    )
    json.dumps(details, allow_nan=False)
    # a line over the bound is not recorded (digests only: not restorable)
    long_line = "- [ ] Long " + "x" * tasksink.MAX_BEFORE_LINE
    (vault / "Notes" / "long.md").write_bytes(long_line.encode() + b"\n")
    long_key = key_of(vault, "Long " + "x" * tasksink.MAX_BEFORE_LINE)
    long = apply(vault, status(long_key, "done")).details
    assert long["changed"] is True
    assert "before_line" not in long
    with pytest.raises(SinkError) as raised:
        restore_status(vault, long)
    assert raised.value.kind == "not_restorable"
    # a line at the bound is recorded
    exact = "- [ ] " + "y" * (tasksink.MAX_BEFORE_LINE - 6)
    (vault / "Notes" / "exact.md").write_bytes(exact.encode() + b"\n")
    exact_key = key_of(vault, "y" * (tasksink.MAX_BEFORE_LINE - 6))
    assert apply(vault, status(exact_key, "done")).details["before_line"] == exact
    # an invalid UTF-8 line is never recorded (it could not travel as JSON unchanged)
    key = key_of(vault, "Call \udcff Ada")
    odd = apply(vault, status(key, "done")).details
    assert odd["changed"] is True
    assert "before_line" not in odd


def test_restore_status_is_served_and_answers_restored(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    assert week(vault) != NOTE
    assert restore_status(vault, details) is True
    assert week(vault) == NOTE
    assert restore_status(vault, details) is False  # already restored
    assert restore_status(vault, {"changed": False}) is False

    async def over_mcp() -> Any:
        plugin = build()
        return await operation(
            plugin, f"{NAME}.restore_status", {"paths": wire(vault).to_wire(), "details": details}
        )

    apply(vault, status(key, "done"))
    assert anyio.run(over_mcp) == {"restored": True}
    assert week(vault) == NOTE


def test_forced_restore_status_over_mcp_passes_force_only_when_true(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    edited(vault, "✅ 2026-11-08", "✅ 2026-11-01")

    async def call(**extra: Any) -> Any:
        return await operation(
            build(),
            f"{NAME}.restore_status",
            {"paths": wire(vault).to_wire(), "details": details, **extra},
        )

    for extra in ({}, {"force": False}):
        reply = anyio.run(lambda extra=extra: call(**extra))  # type: ignore[misc]
        assert reply["error"]["kind"] == "target_changed"
    assert anyio.run(lambda: call(force=True)) == {"restored": True}
    assert week(vault) == NOTE


def test_restore_status_refuses_a_line_edited_since_without_force(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    now = edited(vault, "✅ 2026-11-08", "✅ 2026-11-01")
    for extra in ({}, {"force": False}, {"force": None}, {"force": "yes"}, {"force": 1}):
        with pytest.raises(SinkError) as raised:
            restore_status(vault, details, **extra)
        assert raised.value.kind == "target_changed"
        assert week(vault) == now  # untouched


def test_forced_restore_status_replaces_only_that_line(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    edited(vault, "✅ 2026-11-08", "✅ 2026-11-01")
    edited(vault, "Water", "Water the plants")  # another line, edited too
    now = week(vault)
    assert restore_status(vault, details, force=True) is True
    assert changed_lines(now, week(vault)) == [
        (
            "- [x] Pay rent 📅 2026-11-10 ✅ 2026-11-01 #home\r".encode(),
            "- [ ] Pay rent 📅 2026-11-10 #home\r".encode(),
        )
    ]


def test_forced_restore_status_never_guesses_another_task(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    # the user rewrote the task itself (another description): nothing is overwritten
    now = edited(vault, "Pay rent", "Pay the landlord")
    with pytest.raises(SinkError) as raised:
        restore_status(vault, details, force=True)
    assert raised.value.kind == "target_changed"
    assert week(vault) == now
    # the task line moved: the recorded line number now holds another task
    shifted = b"\xef\xbb\xbf# Week\r\n- [ ] New first\r\n" + now.split(b"\r\n", 1)[1]
    (vault / "Notes" / "week.md").write_bytes(shifted)
    with pytest.raises(SinkError):
        restore_status(vault, details, force=True)
    assert week(vault) == shifted


def test_forced_restore_status_finds_a_moved_task_by_its_block_id(vault: Path) -> None:
    created = apply(
        vault,
        {"type": "task.create", "title": "Linked", "fingerprint": "fp-1"},
        note="Tasks.assistant.md",
    )
    assert created.details is not None
    note = vault / "Tasks.assistant.md"
    key = next(t.key for d in scan(vault).documents for t in d.tasks if "Linked" in t.description)
    details = apply(vault, status(key, "done")).details
    done = note.read_bytes()
    edited_line = done.replace(b"- [x]", b"- [ ] ").replace(b"\xe2\x9c\x85", b"")
    note.write_bytes(b"- [ ] first\n" + edited_line)  # edited, and its line number moved
    with pytest.raises(SinkError) as raised:
        restore_status(vault, details)
    assert raised.value.kind in {"target_changed", "not_restorable"}
    # the line has changed (and moved): forced finds it again by its block id
    note.write_bytes(b"- [ ] first\n" + done.replace(b"Linked", b"Linked now", 1))
    before = note.read_bytes()
    assert restore_status(vault, details, force=True) is True
    assert note.read_bytes().startswith(b"- [ ] first\n")
    assert changed_lines(before, note.read_bytes())[0][0].startswith(b"- [x]")


def test_restore_status_refuses_a_journal_without_before_line(vault: Path) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    old = {k: v for k, v in details.items() if k != "before_line"}  # a pre-#15 journal
    now = week(vault)
    for force in (False, True):
        with pytest.raises(SinkError) as raised:
            restore_status(vault, old, force=force)
        assert raised.value.kind == "not_restorable"
    forged = {**details, "before_line": "- [ ] Something else"}  # not the digest recorded
    with pytest.raises(SinkError) as raised:
        restore_status(vault, forged, force=True)
    assert raised.value.kind == "not_restorable"
    assert week(vault) == now


def test_restore_status_refuses_a_path_outside_the_vault_even_with_force(
    vault: Path, tmp_path: Path
) -> None:
    key = key_of(vault, "Pay rent #home")
    details = apply(vault, status(key, "done")).details
    elsewhere = tmp_path / "other"
    (elsewhere / "Notes").mkdir(parents=True)
    (elsewhere / "Notes" / "week.md").write_bytes(NOTE)  # another vault, the note unchanged
    for bad in ("../other/Notes/week.md", "/etc/passwd", "Notes/../../week.md", ".hidden/x.md"):
        with pytest.raises(SinkError) as raised:
            restore_status(vault, {**details, "path": bad}, force=True)
        assert raised.value.kind in {"path_invalid", "not_restorable", "target_changed"}
    now = week(vault)
    assert now != NOTE  # the vault's own note was never touched by the refusals
    other_vault = Paths(write=(str(elsewhere),))
    # Another vault is the core's `sink_mismatch` (identity check); here it writes nothing
    # because that vault's note holds no changed line.
    wrote = asyncio.run(VaultSink([TASK_TOOL]).restore_status(other_vault, details, force=True))
    assert wrote is False
    assert (elsewhere / "Notes" / "week.md").read_bytes() == NOTE
    assert week(vault) == now


def test_restore_status_in_a_writer_note_relinks_the_index(vault: Path) -> None:
    apply(vault, {"type": "task.create", "title": "Linked", "fingerprint": "fp-2"})
    key = next(t.key for d in scan(vault).documents for t in d.tasks if "Linked" in t.description)
    details = apply(vault, status(key, "done")).details
    assert restore_status(vault, details) is True
    again = apply(vault, status(key, "done"))  # the index agrees with the restored line
    assert again.details["changed"] is True
