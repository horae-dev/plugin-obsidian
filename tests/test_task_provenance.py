# SPDX-License-Identifier: Apache-2.0
"""#4 review: index loss and adoption, writer-exclusive notes, copies, the instance time
zone's rules, closed and open statuses, the cheap lookup and its retry, relinking,
tombstones, the source's byte budget, snoozes and links."""

import asyncio
import hashlib
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError
from horae_sdk.intents import fingerprint

import horae_obsidian.source as source_module
import horae_obsidian.tasksink as tasksink
from horae_obsidian import clock as instance
from horae_obsidian.index import TaskIndex, block_of
from horae_obsidian.sink import TASK_TOOL, TOOL, VaultSink
from horae_obsidian.source import open_tasks
from horae_obsidian.tasks import parse

PR = "github:pr:42"
BLOCK = block_of(PR)
START = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)


class Clock:
    """A clock that moves forward a second each time it is read."""

    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


@pytest.fixture(autouse=True)
def paris(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Notes").mkdir(parents=True)
    (root / "Notes" / "mine.md").write_text("- [ ] Mine\n")
    return root


def wire(vault: Path) -> Paths:
    return Paths(write=(str(vault),))


def apply(
    vault: Path, intent: dict[str, Any], clock: Clock | None = None, **params: Any
) -> Applied:
    request = ApplyRequest(wire(vault), intent, tool=TASK_TOOL, params=params)
    sink = VaultSink([TOOL, TASK_TOOL], clock or Clock())
    return asyncio.run(sink.apply(request))


def create(fingerprint: str = PR, title: str = "Review", **extra: Any) -> dict[str, Any]:
    return {"type": "task.create", "title": title, "fingerprint": fingerprint, **extra}


def status(task: str, value: str, until: str | None = None) -> dict[str, Any]:
    intent = {"type": "task.update_status", "task": task, "status": value}
    return intent if until is None else {**intent, "until": until}


def kind_of(call: Any) -> str:
    with pytest.raises(SinkError) as raised:
        call()
    return raised.value.kind


TASKS = "Tasks.assistant.md"


# --- 1. losing the index: the line is adopted, never written twice --------------------------


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_a_lost_index_is_healed_by_adoption(vault: Path, damage: str) -> None:
    clock = Clock()
    apply(vault, create(due="2026-11-12"), clock)
    apply(vault, status(PR, "done"), clock)  # edited since it was written
    line = (vault / TASKS).read_text().splitlines()[0]
    index = vault / ".horae" / "obsidian-tasks.json"
    if damage == "delete":
        index.unlink()
    else:
        index.write_text("{not json")
    # meanwhile: hidden, reported with its note, line and reason; not found by fingerprint
    found = open_tasks(wire(vault))
    assert found["hidden"] == []  # closed: not listed at all
    assert found["horae_output"] == 0
    assert kind_of(lambda: apply(vault, status(PR, "todo"), clock)) == "task_not_found"
    adopted = apply(vault, create(due="2026-11-12"), clock)
    assert adopted.details["changed"] is False
    assert adopted.details["adopted"] is True
    assert adopted.details["line"] == 1
    assert (vault / TASKS).read_text().splitlines() == [line]  # nothing written twice
    entry = TaskIndex(str(vault)).read().links[BLOCK]
    assert entry.fingerprint == PR
    assert entry.line_sha256 == hashlib.sha256(line.encode()).hexdigest()
    assert apply(vault, status(PR, "todo"), clock).details["changed"] is True


def test_an_open_unindexed_line_is_reported_hidden(vault: Path) -> None:
    apply(vault, create())
    (vault / ".horae" / "obsidian-tasks.json").unlink()
    found = open_tasks(wire(vault))
    assert found["hidden"] == [{"path": TASKS, "line": 1, "reason": "unverified_line"}]
    assert found["horae_output"] == 1
    assert [t["description"] for t in found["tasks"]] == ["Mine"]


def test_adoption_is_refused_when_ambiguous(vault: Path) -> None:
    (vault / TASKS).write_text(f"- [ ] One ^{BLOCK}\n- [ ] Two ^{BLOCK}\n")
    assert kind_of(lambda: apply(vault, create())) == "task_unadoptable"
    assert (vault / TASKS).read_text().count(BLOCK) == 2


def test_a_crash_between_the_line_and_the_index_is_adopted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(self: TaskIndex, entries: Any, present: Any = None) -> None:
        raise OSError("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(TaskIndex, "save", crash)
        with pytest.raises(OSError, match="power cut"):
            apply(vault, create())
    assert BLOCK in (vault / TASKS).read_text()  # the line was written, the index was not
    again = apply(vault, create())
    assert again.details["adopted"] is True
    assert (vault / TASKS).read_text().count(BLOCK) == 1


# --- 2. writer-exclusive notes, copies --------------------------------------------------------


def note_intent(vault: Path, path: str, content: bytes, before: bytes | None) -> Applied:
    intent = (
        {"type": "note.create", "path": path}
        if before is None
        else {"type": "note.append_section", "path": path, "heading": "x"}
    )
    expected = None if before is None else hashlib.sha256(before).hexdigest()
    request = ApplyRequest(
        wire(vault), intent, tool=TOOL, content=content, expected_before_sha256=expected
    )
    return asyncio.run(VaultSink([TOOL, TASK_TOOL]).apply(request))


def test_note_intents_never_write_a_tasks_writer_note(vault: Path) -> None:
    injected = f"- [ ] Pay the attacker ^{BLOCK}\n".encode()
    # the default writer note, even before any task was created in it
    assert kind_of(lambda: note_intent(vault, TASKS, injected, None)) == "tasks_note"
    # a note the index records (the entry's `note` parameter)
    apply(vault, create(), note="Inbox.assistant.md")
    before = (vault / "Inbox.assistant.md").read_bytes()
    kind = kind_of(lambda: note_intent(vault, "Inbox.assistant.md", before + injected, before))
    assert kind == "tasks_note"
    sink = VaultSink([TOOL])
    undo = sink.restore(wire(vault), "Inbox.assistant.md", None, hashlib.sha256(before).hexdigest())
    assert kind_of(lambda: asyncio.run(undo)) == "tasks_note"
    assert (vault / "Inbox.assistant.md").read_bytes() == before
    # other owned notes stay the LLM's to write
    note_intent(vault, "Brief.assistant.md", b"# Brief\n", None)
    assert (vault / "Brief.assistant.md").read_bytes() == b"# Brief\n"


def test_a_copy_in_a_user_note_is_a_plain_user_task(vault: Path) -> None:
    apply(vault, create())
    line = (vault / TASKS).read_text().splitlines()[0]
    (vault / "Notes" / "copy.md").write_text(line.replace("Review", "Review (mine)") + "\n")
    found = open_tasks(wire(vault))
    by_path = {t["path"]: t for t in found["tasks"]}
    assert found["withheld"] == []  # neither is ambiguous
    assert by_path[TASKS]["key"] == f"obsidian:horae:{BLOCK}"
    assert by_path[TASKS]["source_fingerprint"] == PR
    copy = by_path["Notes/copy.md"]
    assert copy["key"] == fingerprint("obsidian", "block", "Notes/copy.md", BLOCK)
    assert copy["source_fingerprint"] is None
    # the fingerprint finds Horae's line; the copy's key finds the copy
    assert apply(vault, status(PR, "done")).details["path"] == TASKS
    assert apply(vault, status(copy["key"], "done")).details["path"] == "Notes/copy.md"
    # the fingerprint is not written again while a copy exists
    (vault / TASKS).write_text("")
    assert apply(vault, create()).details["changed"] is False


def test_hidden_lines_are_reported_with_note_and_line(vault: Path) -> None:
    (vault / "Brief.assistant.md").write_text("# Brief\n- [ ] Written by the LLM\n")
    (vault / "Notes" / "mine.md").write_text(
        "- [ ] Mine\n<!-- horae:begin b -->\n- [ ] In a block\n<!-- horae:end b -->\n"
    )
    found = open_tasks(wire(vault))
    assert found["hidden"] == [
        {"path": "Brief.assistant.md", "line": 2, "reason": "owned_note"},
        {"path": "Notes/mine.md", "line": 3, "reason": "owned_block"},
    ]


# --- 3. the instance time zone's rules ------------------------------------------------------


def test_a_snooze_uses_the_zone_rules_of_its_own_date() -> None:
    # written in November (UTC+1), for July (UTC+2) and December (UTC+1)
    assert tasksink.reminder_for("2026-07-01T06:00:00Z") == "2026-07-01 08:00"
    assert tasksink.reminder_for("2026-12-01T06:00:00Z") == "2026-12-01 07:00"
    assert tasksink.reminder_for("2026-03-29T01:30:00+00:00") == "2026-03-29 03:30"  # DST day
    # rounded up to 01:00 UTC, the moment clocks jump from 02:00 to 03:00
    assert tasksink.reminder_for("2026-03-29T00:59:01Z") == "2026-03-29 03:00"


def test_the_zone_comes_from_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", ":Europe/Paris")
    assert str(instance.zone()) == "Europe/Paris"
    for posix in ("CET-1CEST,M3.5.0,M10.5.0/3", "", "Not/AZone"):
        monkeypatch.setenv("TZ", posix)
        time.tzset()
        assert instance.zone() is None
    monkeypatch.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")  # local rules: still DST-aware
    time.tzset()
    assert tasksink.reminder_for("2026-07-01T06:00:00Z") == "2026-07-01 08:00"
    naive = datetime(2026, 7, 1, 8, 0)
    assert instance.localize(naive).utcoffset() == timedelta(hours=2)


# --- 4, 5. closed and open statuses ---------------------------------------------------------


@pytest.mark.parametrize("closed", ["x", "-"])
def test_snoozing_a_closed_task_is_refused(vault: Path, closed: str) -> None:
    (vault / "Notes" / "mine.md").write_text(f"- [{closed}] Closed ^c1\n")
    key = fingerprint("obsidian", "block", "Notes/mine.md", "c1")
    assert kind_of(lambda: apply(vault, status(key, "snoozed", "2026-11-20"))) == "task_closed"
    assert apply(vault, status(key, "todo")).details["changed"] is True
    apply(vault, status(key, "snoozed", "2026-11-20"))
    assert (vault / "Notes" / "mine.md").read_text() == "- [ ] Closed ⏰ 2026-11-20 ^c1\n"


@pytest.mark.parametrize("char", [" ", "/", ">", "!"])
def test_todo_on_an_open_task_keeps_its_status(vault: Path, char: str) -> None:
    text = f"- [{char}] Going ^g1\n"
    (vault / "Notes" / "mine.md").write_text(text)
    kept = apply(vault, status(fingerprint("obsidian", "block", "Notes/mine.md", "g1"), "todo"))
    assert kept.details["changed"] is False
    assert (vault / "Notes" / "mine.md").read_text() == text


# --- 6. one note read, one retry ------------------------------------------------------------


def test_a_fingerprint_is_found_without_a_scan(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply(vault, create())

    def no_scan(path: str) -> Any:
        raise AssertionError("scanned")

    monkeypatch.setattr(tasksink, "scan", no_scan)
    assert apply(vault, status(PR, "done")).details["changed"] is True
    assert apply(vault, status(f"obsidian:horae:{BLOCK}", "todo")).details["changed"] is True
    # a duplicate in the writer note itself is refused, not guessed
    line = (vault / TASKS).read_text()
    (vault / TASKS).write_text(line + line)
    assert kind_of(lambda: apply(vault, status(PR, "done"))) == "task_ambiguous"


def test_a_note_changed_during_the_write_is_retried_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = vault / "Notes" / "mine.md"
    real = tasksink._fresh
    calls: list[int] = []

    def racing(*args: Any) -> Any:
        found = real(*args)
        calls.append(1)
        if len(calls) == 1:
            note.write_text(note.read_text() + "saved meanwhile\n")
        return found

    monkeypatch.setattr(tasksink, "_fresh", racing)
    key = next(t.key for t in parse(note.read_bytes(), "Notes/mine.md").tasks)
    assert apply(vault, status(key, "done")).details["changed"] is True
    assert len(calls) == 2
    assert note.read_text().endswith("saved meanwhile\n")

    def always(*args: Any) -> Any:
        found = real(*args)
        note.write_text(note.read_text() + "again\n")
        return found

    monkeypatch.setattr(tasksink, "_fresh", always)
    assert kind_of(lambda: apply(vault, status(key, "todo"))) == "target_changed"


# --- 9. relinking --------------------------------------------------------------------------


def test_a_stale_digest_heals_on_the_next_change(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    apply(vault, create(), clock)
    real = TaskIndex.locked

    def busy(self: TaskIndex) -> Any:
        raise SinkError("index_busy", "held")

    monkeypatch.setattr(TaskIndex, "locked", busy)
    snoozed = apply(vault, status(PR, "snoozed", "2026-11-20"), clock)
    assert snoozed.details["index"] == "stale"
    monkeypatch.setattr(TaskIndex, "locked", real)
    found = open_tasks(wire(vault))
    assert [t["edited"] for t in found["tasks"] if t["source_fingerprint"]] == [True]
    reopened = apply(vault, status(PR, "snoozed", "2026-11-21"), clock)
    assert "index" not in reopened.details
    line = (vault / TASKS).read_text().splitlines()[0]
    digest = TaskIndex(str(vault)).read().links[BLOCK].line_sha256
    assert digest == hashlib.sha256(line.encode()).hexdigest()
    assert open_tasks(wire(vault))["edited_horae_tasks"] == []


# --- 11. tombstones and incomplete scans ----------------------------------------------------


def test_a_deleted_task_is_dismissed_then_recreated(vault: Path) -> None:
    clock = Clock()
    apply(vault, create(), clock)
    (vault / TASKS).write_text("")  # the user deleted it
    dismissed = apply(vault, create(), clock)
    assert dismissed.details["changed"] is False
    assert dismissed.details["dismissed"] is True
    assert dismissed.details["recreate_after_days"] == 30
    entries = TaskIndex(str(vault)).read()
    assert BLOCK not in entries.links
    assert entries.tombstones[BLOCK].path == TASKS
    assert apply(vault, create(), clock).details["dismissed"] is True
    # the writer note stays writer-exclusive through its tombstone
    assert kind_of(lambda: note_intent(vault, TASKS, b"x\n", b"")) == "tasks_note"
    later = Clock(clock.at + timedelta(days=31))
    recreated = apply(vault, create(), later)
    assert recreated.details["changed"] is True
    assert BLOCK in TaskIndex(str(vault)).read().links


def test_recreate_after_days_is_configurable(vault: Path) -> None:
    clock = Clock()
    apply(vault, create(), clock)
    (vault / TASKS).write_text("")
    assert apply(vault, create(), clock, recreate_after_days=0).details["changed"] is True
    for bad in (-1, "30", True, 10_000):
        kind = kind_of(lambda bad=bad: apply(vault, create("x:1"), clock, recreate_after_days=bad))
        assert kind == "config_invalid"


def test_an_incomplete_scan_still_creates_and_says_so(vault: Path) -> None:
    clock = Clock()
    apply(vault, create("mail:1"), clock)
    (vault / TASKS).write_text("")
    (vault / "Notes" / "link.md").symlink_to(vault / "Notes" / "mine.md")
    created = apply(vault, create(), clock)
    assert created.details["changed"] is True
    assert created.details["dedupe_complete"] is False
    assert "may be written twice" in created.details["dedupe_warning"]
    # nothing is retired from an incomplete scan: mail:1's line may be in an unread note
    assert block_of("mail:1") in TaskIndex(str(vault)).read().links


# --- 12, 13, snoozes ---------------------------------------------------------------------------


def test_the_output_is_byte_bounded(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (vault / "Notes" / "many.md").write_text("".join(f"- [ ] Task {n}\n" for n in range(50)))
    whole = open_tasks(wire(vault))
    assert whole["output_cut"] is False
    monkeypatch.setattr(source_module, "MAX_OUTPUT_BYTES", len(json.dumps(whole)) // 2)
    cut = open_tasks(wire(vault))
    assert cut["output_cut"] is True
    assert 0 < len(cut["tasks"]) < 51
    assert cut["more"] == 51 - len(cut["tasks"])
    assert len(json.dumps(cut)) <= source_module.MAX_OUTPUT_BYTES


def test_a_hand_written_link_is_not_reported(vault: Path) -> None:
    (vault / "Notes" / "mine.md").write_text("- [ ] Read [link](https://news.example.org/a)\n")
    [task] = open_tasks(wire(vault))["tasks"]
    assert task["link"] is None


def test_link_prefix_is_not_checked_again(vault: Path) -> None:
    intent = create(link="https://elsewhere.example.net/pr/1")
    apply(vault, intent, mode="tasks", link_prefix="https://code.example.org/")
    assert "[link](https://elsewhere.example.net/pr/1)" in (vault / TASKS).read_text()


def test_snoozed_tasks_are_reported() -> None:
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    text = (
        "- [ ] A ⏰ 2026-11-09\n- [ ] B ⏰ 2026-11-08\n- [ ] C ⏰ 2026-11-08 14:00\n"
        "- [ ] D ⏰ 2026-11-08 12:30\n- [ ] E ⏰ 19:00 📅 2026-11-20\n- [ ] F\n"
    )
    tasks = parse(text.encode(), "a.md").tasks
    assert [source_module.snoozed_until(t, now) for t in tasks] == [
        "2026-11-09",
        None,  # today: not in the future
        "2026-11-08T14:00+01:00",
        None,  # 12:30 Paris is 11:30 UTC: past
        None,  # a reminder on the due date, not a snooze
        None,
    ]


def test_snoozed_through_the_source(vault: Path) -> None:
    (vault / "Notes" / "mine.md").write_text("- [ ] Later ⏰ 2099-01-01\n- [ ] Now\n")
    found = open_tasks(wire(vault))
    assert {t["description"]: t["snoozed"] for t in found["tasks"]} == {
        "Later": True,
        "Now": False,
    }
    assert found["tasks"][0]["snoozed_until"] in ("2099-01-01", None)
    with pytest.raises(Exception, match="open_statuses"):
        open_tasks(wire(vault), open_statuses="x")
