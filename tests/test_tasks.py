# SPDX-License-Identifier: Apache-2.0
"""The Obsidian Tasks parser on the synthetic vault (`tests/fixtures/vault`).

Acceptance criteria of #2: the parser handles the Obsidian Tasks emoji format (statuses,
every signifier with and without U+FE0F, the ⏰ reminder, tags, hidden ids, nesting, list
markers, blockquotes and callouts, code and frontmatter); unknown syntax is preserved, never
rewritten (the round trip is byte-exact, bad values are kept as unknown)."""

import time
from datetime import date
from pathlib import Path

import horae_sdk.tasks as sdk
import pytest
from horae_sdk.tasks import escape_title, task_line

from horae_obsidian.tasks import (
    SIGNIFIERS,
    VS16,
    Document,
    Reminder,
    Task,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures"
VAULT = FIXTURES / "vault"


def load(name: str) -> Document:
    return parse((VAULT / name).read_bytes(), name)


def by_description(document: Document) -> dict[str, Task]:
    return {task.description: task for task in document.tasks}


# --- the signifier set agrees with the SDK ---------------------------------------------


def test_signifiers_are_the_sdks() -> None:
    """The reading side knows exactly the signifiers the SDK escapes, with the same meaning."""
    assert set(SIGNIFIERS) == set(sdk.SIGNIFIERS)
    meaning = {
        "due": "due",
        "scheduled": "scheduled",
        "start": "start",
        "created": "created",
        "done": "done",
        "cancelled": "cancelled",
        "recurrence": "recurrence",
        "reminder": "reminder",
        "id": "id",
        "depends_on": "depends on",
        "on_completion": "on completion",
    }
    for emoji, kind in SIGNIFIERS.items():
        expected = sdk.SIGNIFIERS[emoji]
        assert expected.endswith("priority") if kind == "priority" else meaning[kind] == expected
    assert sdk.DUE in SIGNIFIERS
    assert sdk.REMINDER in SIGNIFIERS


# --- statuses ---------------------------------------------------------------------------


def test_statuses() -> None:
    tasks = by_description(load("statuses.md"))
    assert {d: (t.status_char, t.status) for d, t in tasks.items()} == {
        "Water the plants": (" ", "todo"),
        "Buy bread": ("x", "done"),
        "Call the plumber": ("X", "done"),
        "Write the report": ("/", "in_progress"),
        "Old idea": ("-", "cancelled"),
        "Forwarded to next week": (">", "custom"),
        "Question for the team": ("?", "custom"),
    }
    assert tasks["Buy bread"].done == date(2026, 10, 20)
    assert tasks["Old idea"].cancelled == date(2026, 10, 1)


# --- signifiers -------------------------------------------------------------------------


def test_every_date_signifier() -> None:
    tasks = by_description(load("signifiers.md"))
    assert tasks["Due"].due == date(2026, 10, 22)
    assert tasks["Due calendar"].due == date(2026, 10, 23)
    assert tasks["Due spiral"].due == date(2026, 10, 24)
    assert tasks["Due spiral"].fields[0].signifier == "\U0001f5d3" + VS16
    assert tasks["Due spiral bare"].due == date(2026, 10, 25)
    assert tasks["Due spiral bare"].fields[0].signifier == "\U0001f5d3"
    assert tasks["Scheduled"].scheduled == date(2026, 10, 26)
    assert tasks["Scheduled sand"].scheduled == date(2026, 10, 27)
    assert tasks["Start"].start == date(2026, 10, 28)
    assert tasks["Created"].created == date(2026, 10, 1)
    assert tasks["Done"].done == date(2026, 10, 29)
    assert tasks["Cancelled"].cancelled == date(2026, 10, 30)
    assert tasks["No space"].due == date(2026, 11, 3)


def test_other_signifiers() -> None:
    tasks = by_description(load("signifiers.md"))
    assert tasks["Recurring"].recurrence == "every week on Monday"
    assert [tasks[p.capitalize()].priority for p in ("highest", "high", "medium", "low")] == [
        "highest",
        "high",
        "medium",
        "low",
    ]
    assert tasks["Lowest"].priority == "lowest"
    assert tasks["Has an id"].id == "abc-123"
    assert tasks["Depends"].depends_on == ("abc-123", "def_456")
    assert tasks["Depends with selector"].depends_on == ("abc-123",)
    assert tasks["On completion"].on_completion == "delete"
    assert tasks["Due"].priority is None


def test_all_fields_on_one_line_and_variation_selectors() -> None:
    tasks = by_description(load("signifiers.md"))
    everything = tasks["Everything #home"]
    assert [f.kind for f in everything.fields] == [
        "priority",
        "recurrence",
        "created",
        "start",
        "scheduled",
        "due",
        "reminder",
        "id",
        "depends_on",
        "on_completion",
    ]
    assert everything.recurrence == "every day"
    assert everything.due == date(2026, 10, 4)
    assert everything.reminder == Reminder(None, "08:30")
    assert everything.id == "all-1"
    assert everything.on_completion == "keep"
    assert everything.block_id == "everything"
    assert everything.tags == ("#home",)
    selectors = tasks["Selectors"]
    assert selectors.priority == "high"
    assert selectors.due == date(2026, 11, 1)
    assert selectors.done == date(2026, 11, 2)
    assert [f.signifier[-1] for f in selectors.fields] == [VS16] * 3


def test_field_spans_point_at_their_text() -> None:
    for name in ("signifiers.md", "reminders.md", "invalid.md"):
        for task in load(name).tasks:
            for found in task.fields:
                assert task.line.text[found.start : found.end] == found.text
                assert found.text.startswith(found.signifier)


# --- reminders --------------------------------------------------------------------------


def test_reminders() -> None:
    """Horae writes `⏰ HH:MM` after the due date (`horae_sdk.tasks.task_line`); the Reminder
    plugin's `⏰ YYYY-MM-DD HH:MM` and `⏰ YYYY-MM-DD` are read too."""
    tasks = by_description(load("reminders.md"))
    assert tasks["Pay the bill"].reminder == Reminder(None, "19:00")
    assert tasks["Pay the bill"].due == date(2026, 10, 22)
    assert tasks["Reminder plugin"].reminder == Reminder(date(2026, 10, 21), "09:15")
    assert tasks["Reminder date only"].reminder == Reminder(date(2026, 10, 21), None)
    bad = tasks["Bad reminder time"]
    assert bad.reminder is None
    assert bad.unknown == ("⏰ 25:00",)
    assert bad.due == date(2026, 10, 22)


def test_the_sdk_task_line_parses_back() -> None:
    line = task_line("Pay the bill", due="2026-10-22", time="19:00")
    [task] = parse(line.encode()).tasks
    assert (task.description, task.due, task.reminder) == (
        "Pay the bill",
        date(2026, 10, 22),
        Reminder(None, "19:00"),
    )
    assert task.status == "todo"
    [done] = parse(task_line("x", done=True).encode()).tasks
    assert done.status == "done"


# --- tags -------------------------------------------------------------------------------


def test_tags() -> None:
    plain, nots, unicode = load("tags.md").tasks
    assert plain.tags == ("#home", "#work/project", "#trailing")
    assert plain.description == "Plain #home and #work/project tags #trailing"
    assert nots.tags == ()
    assert unicode.tags == ("#café", "#🏡")


def test_headings_are_not_tasks() -> None:
    document = load("tags.md")
    assert all("not-a-task-tag" not in tag for task in document.tasks for tag in task.tags)
    assert parse(b"# - [ ] heading #tag\n## [ ] x\n").tasks == ()


# --- hidden ids and stable keys ---------------------------------------------------------


def test_hidden_ids() -> None:
    tasks = by_description(load("ids.md"))
    assert tasks["With a Tasks id"].id == "water-1"
    assert tasks["With a Tasks id"].key == "obsidian:id:water-1"
    assert tasks["With a block id"].block_id == "water-2"
    assert tasks["With a block id"].key.startswith("obsidian:block:ids.md:water-2")
    both = tasks["With both"]
    assert (both.id, both.block_id, both.key) == ("water-3", "water-3b", "obsidian:id:water-3")


def test_keys_are_unique_and_in_the_fingerprint_alphabet() -> None:
    document = load("ids.md")
    keys = [task.key for task in document.tasks]
    assert len(set(keys)) == len(keys)  # "Twice the same" twice: two occurrences
    for key in keys:
        assert len(key) <= 128
        assert all(c.isascii() and (c.isalnum() or c in "._:-") for c in key)


def test_keys_survive_ticking_dates_and_lines_above() -> None:
    before = parse(b"- [ ] Water the plants \xf0\x9f\x93\x85 2026-10-22\n", "a.md")
    ticked = parse(
        "# New heading\n\nSome text\n- [x] Water the plants 📅 2026-10-25 ✅ 2026-10-23\n".encode(),
        "a.md",
    )
    assert before.tasks[0].key == ticked.tasks[0].key
    assert parse(b"- [ ] Water the plants\n", "b.md").tasks[0].key != before.tasks[0].key
    assert parse(b"- [ ] Water the cat\n", "a.md").tasks[0].key != before.tasks[0].key


# --- nesting and list markers -----------------------------------------------------------


def test_nesting_and_markers() -> None:
    document = load("nesting.md")
    tasks = by_description(document)
    assert {d: (t.marker, t.depth) for d, t in tasks.items()} == {
        "Parent": ("-", 0),
        "Child with four spaces": ("-", 1),
        "Grandchild": ("-", 2),
        "Child with a tab": ("-", 1),
        "Under a plain item": ("-", 1),
        "Star marker": ("*", 0),
        "Plus marker": ("+", 0),
        "Numbered with a dot": ("1.", 0),
        "Numbered with a parenthesis": ("2)", 0),
        "After the paragraph": ("-", 0),
        # Tasks' own rule: `\[(.)\]\s*(.*)`, no space needed after the box
        "no space after the box": ("-", 0),
        "(https://example.org) a link": ("-", 0),
    }
    assert tasks["Child with four spaces"].parent == tasks["Parent"].line.number
    assert tasks["Grandchild"].parent == tasks["Child with four spaces"].line.number
    assert tasks["Child with a tab"].parent == tasks["Parent"].line.number
    assert tasks["Under a plain item"].parent == tasks["Under a plain item"].line.number - 1
    assert tasks["Child with four spaces"].prefix == "    "
    assert tasks["After the paragraph"].parent is None


def test_lines_that_only_look_like_tasks() -> None:
    descriptions = {task.description for task in load("nesting.md").tasks}
    assert not {d for d in descriptions if "marker" in d and "no space" in d}
    assert not {d for d in descriptions if "empty box" in d or "two characters" in d}
    assert parse(b"- [ ]\n").tasks[0].description == ""
    [glued] = parse(b"- [x]x\n").tasks
    assert (glued.status, glued.description) == ("done", "x")
    assert parse(b"-\t[ ] a tab after the marker\n").tasks == ()
    assert parse(b"- \t[ ] a tab after the marker\n").tasks == ()


# --- blockquotes, callouts, code, frontmatter, comments ---------------------------------


def test_blockquotes_and_callouts() -> None:
    tasks = by_description(load("quotes.md"))
    assert set(tasks) == {
        "In a blockquote",
        "Nested blockquote",
        "In a callout",
        "Nested in a callout",
    }
    assert tasks["In a blockquote"].prefix == "> "
    assert tasks["In a blockquote"].due == date(2026, 10, 22)
    assert tasks["Nested blockquote"].prefix == "> > "
    assert tasks["In a callout"].reminder == Reminder(date(2026, 10, 22), "07:00")
    assert tasks["Nested in a callout"].parent == tasks["In a callout"].line.number


def test_code_frontmatter_and_comment_blocks_hold_no_task() -> None:
    tasks = load("code.md").tasks
    assert [t.description for t in tasks] == [
        "A real task after the code",
        "Inline comment %% hidden %% stays a task",
    ]
    assert tasks[0].due == date(2026, 10, 22)


def test_unclosed_frontmatter_and_fences() -> None:
    assert len(parse(b"---\n- [ ] not frontmatter: never closed\n").tasks) == 1
    assert parse(b"```\n- [ ] in a fence never closed\n").tasks == ()
    assert len(parse(b"````\n```\n- [ ] in\n````\n- [ ] out\n").tasks) == 1
    assert len(parse(b"``` x ` y\n- [ ] not a fence: backtick in the info\n").tasks) == 1


# --- unknown syntax is preserved, never rewritten ---------------------------------------


def test_values_that_do_not_parse_are_kept_as_unknown() -> None:
    tasks = load("invalid.md").tasks
    bad_day, bad_month, not_a_date, mid_line, emoji, inline = tasks
    assert bad_day.due is None
    assert bad_day.unknown == ("📅 2026-02-30",)
    assert bad_month.done is None
    assert bad_month.unknown == ("✅ 2026-13-01",)
    assert bad_month.due == date(2026, 10, 22)
    assert not_a_date.fields == ()
    assert not_a_date.description == "Not a date 📅 tomorrow"
    assert mid_line.due is None
    assert mid_line.description == "Mid-line 📅 2026-10-22 then more text"
    assert emoji.fields == ()
    assert inline.fields == ()
    assert inline.description == "Inline field [due:: 2026-10-22]"


@pytest.mark.parametrize("path", sorted(p.name for p in VAULT.rglob("*.md")))
def test_every_fixture_round_trips_byte_for_byte(path: str) -> None:
    data = next(VAULT.rglob(path)).read_bytes()
    document = parse(data, path)
    assert document.render() == data
    for line in document.lines:
        assert data[line.start : line.end] == line.text.encode("utf-8", "surrogateescape")
    for task in document.tasks:
        assert task.render() == task.line.text
        assert document.replace_line(task, task.render()) == data


def test_crlf_and_bom_are_kept() -> None:
    crlf = parse((VAULT / "Projects" / "crlf.md").read_bytes(), "Projects/crlf.md")
    assert {line.eol for line in crlf.lines} == {"\r\n"}
    assert crlf.tasks[0].due == date(2026, 10, 22)
    bom = load("bom.md")
    assert bom.bom
    assert bom.lines[0].start == 3
    assert bom.tasks[0].description == "First line after a BOM"


def test_invalid_utf8_is_kept_without_crashing() -> None:
    document = load("invalid-utf8.md")
    assert document.invalid_utf8
    latin, cut, after = document.tasks
    assert latin.due == date(2026, 10, 22)
    assert latin.description == "Latin-1 caf\udce9"
    assert cut.fields == ()
    assert after.status == "done"
    assert document.lines[-1].eol == ""
    assert not load("statuses.md").invalid_utf8


def test_a_status_edit_changes_only_that_line() -> None:
    """What #4's `task.update_status` sink does: only the status character's bytes change."""
    data = (VAULT / "Projects" / "crlf.md").read_bytes()
    document = parse(data, "Projects/crlf.md")
    task = document.tasks[0]
    edited = document.replace_line(task, task.with_status("x"))
    assert edited == data.replace(b"- [ ] Windows", b"- [x] Windows")
    assert parse(edited, "Projects/crlf.md").tasks[0].key == task.key
    with pytest.raises(ValueError, match="line break"):
        document.replace_line(task, "a\nb")
    for bad in ("", "xx", "\n", "\t", "\x00", "\u202e", "\udce9", "\u2028"):
        with pytest.raises(ValueError, match="status"):
            task.with_status(bad)
    other = parse(b"x\n- [ ] other\n")
    with pytest.raises(ValueError, match="not a line"):
        document.replace_line(other.tasks[0], "- [x] other")


def test_empty_and_lone_lines() -> None:
    assert parse(b"").render() == b""
    assert len(parse(b"").lines) == 1
    assert [line.eol for line in parse(b"a\rb\r\nc\n").lines] == ["\r", "\r\n", "\n"]


# --- hostile titles (threat model T02) --------------------------------------------------


def test_a_title_escaped_by_the_sdk_parses_back_with_no_field() -> None:
    title = (FIXTURES / "hostile-title.txt").read_text("utf-8").rstrip("\n")
    lines = (VAULT / "hostile.md").read_text("utf-8").splitlines()
    assert lines[2:] == [task_line(title, due="2030-01-01"), task_line(title)]
    with_due, bare = load("hostile.md").tasks
    assert [f.kind for f in with_due.fields] == ["due"]
    assert with_due.due == date(2030, 1, 1)
    for task in (with_due, bare):
        assert task.description == escape_title(title)
        assert task.tags == ()
        assert task.block_id is None
        assert task.id is None
        assert task.reminder is None
        assert task.unknown == ()
    assert bare.fields == ()
    # the same title, not escaped, would set every one of them
    [raw] = parse(f"- [ ] {title}".encode()).tasks
    assert raw.due == date(2020, 1, 1)
    assert raw.id == "evil"
    assert raw.block_id == "blk"
    assert raw.recurrence == "every day"
    assert "#t" in raw.tags


def test_a_long_line_parses_in_linear_time() -> None:
    line = "- [ ] " + "a 🔁 a#b " * 1_400 + "📅 2026-10-22"
    started = time.monotonic()
    [task] = parse(line.encode()).tasks
    assert time.monotonic() - started < 1
    assert task.due == date(2026, 10, 22)
