# SPDX-License-Identifier: Apache-2.0
"""Task identity and the edits #4 makes: keys (NFC, whitespace, duplicates, fallbacks), line
and note hashes, `tick` / `untick`, the JSON projection, headings, owned blocks, warnings."""

import hashlib
import json
from datetime import date

import pytest

import horae_obsidian.tasks as tasks_module
from horae_obsidian.tasks import (
    MAX_ID,
    MAX_LINE_BYTES,
    RecurringTaskError,
    Task,
    parse,
)

KEY_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


def one(text: str, path: str = "a.md") -> Task:
    [task] = parse(text.encode("utf-8", "surrogateescape"), path).tasks
    return task


# --- keys -------------------------------------------------------------------------------


def test_keys_use_the_nfc_path_and_description() -> None:
    nfc, nfd = "café", "café"
    assert one(f"- [ ] {nfc}", f"{nfc}.md").key == one(f"- [ ] {nfd}", f"{nfd}.md").key
    assert one(f"- [ ] {nfd} ^b", f"{nfd}.md").key == one("- [ ] x ^b", f"{nfc}.md").key
    task = one("- [ ] x", f"{nfd}.md")
    assert task.path == f"{nfd}.md"  # the raw path is kept for I/O


def test_only_spaces_and_tabs_are_collapsed_in_the_digest() -> None:
    base = one("- [ ] Water the plants").key
    assert one("- [ ] Water  the\tplants").key == base
    assert one("- [ ] Water\u2028the plants").key != base  # a line separator is text
    assert one("- [ ] Water\u00a0the plants").key != base


def test_keys_change_only_with_the_description_path_or_id() -> None:
    base = one("- [ ] Pay rent 📅 2026-10-22 #home").key
    # status, dates, priority and done dates, even with the trailing tag among them
    assert one("- [x] Pay rent 📅 2026-11-22 ⏫ #home ✅ 2026-10-23").key == base
    assert one("- [x] Pay rent #home 📅 2026-11-22 ⏫ ✅ 2026-10-23").key == base
    assert one("- [ ] Pay #home rent 📅 2026-10-22").key != base  # a tag inside the text
    assert one("- [ ] Pay rent #home", "b.md").key != base
    assert one("- [ ] Pay the rent #home").key != base
    assert one("- [ ] Pay rent #home 🆔 r1").key == "obsidian:id:r1"


def test_every_task_counts_towards_occurrences() -> None:
    """Adding or removing a 🆔 on one copy never moves the other copy's key."""
    with_id = parse("- [ ] Same 🆔 a1\n- [ ] Same\n".encode(), "a.md").tasks
    without = parse(b"- [ ] Same\n- [ ] Same\n", "a.md").tasks
    assert with_id[1].key == without[1].key
    assert with_id[0].key == "obsidian:id:a1"
    assert without[0].key != without[1].key


def test_duplicate_ids_and_block_ids_are_flagged() -> None:
    document = parse(
        "- [ ] A 🆔 same\n- [ ] B 🆔 same\n- [ ] C ^blk\n- [ ] D ^blk\n- [ ] E\n".encode(), "a.md"
    )
    assert [t.key_unique for t in document.tasks] == [False, False, False, False, True]
    assert document.duplicate_keys == tuple(sorted({document.tasks[0].key, document.tasks[2].key}))
    assert parse(b"- [ ] A\n- [ ] A\n").duplicate_keys == ()


def test_ids_longer_than_the_cap_are_text() -> None:
    longest = "a" * MAX_ID
    assert one(f"- [ ] x 🆔 {longest}").id == longest
    too_long = one(f"- [ ] x 🆔 {longest}b")
    assert too_long.id is None
    assert too_long.description == f"x 🆔 {longest}b"
    assert one(f"- [ ] x ^{longest}").block_id == longest
    assert one(f"- [ ] x ^{longest}b").block_id is None
    assert one(f"- [ ] x ⛔ a,{longest}b").depends_on == ()


def test_a_fingerprint_refusal_falls_back_to_the_digest_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tasks_module.fingerprint  # type: ignore[attr-defined]

    def refuse_ids(*parts: str) -> str:
        if parts[1] in ("id", "block"):
            raise ValueError("refused")
        return real(*parts)

    monkeypatch.setattr(tasks_module, "fingerprint", refuse_ids)
    expected = one("- [ ] x").key
    assert one("- [ ] x 🆔 a").key == expected
    assert one("- [ ] x ^b").key == expected

    def refuse_all(*parts: str) -> str:
        raise ValueError("refused")

    monkeypatch.setattr(tasks_module, "fingerprint", refuse_all)
    key = one("- [ ] x").key
    assert key.startswith("obsidian:task::")
    assert set(key) <= KEY_ALPHABET


def test_hashes_for_compare_and_swap() -> None:
    data = b"# T\r\n- [ ] x \xe9\r\n"
    document = parse(data, "a.md", mtime_ns=5, ino=7)
    assert document.sha256 == hashlib.sha256(data).hexdigest()
    assert (document.size, document.mtime_ns, document.ino) == (len(data), 5, 7)
    [task] = document.tasks
    assert task.line_sha256 == hashlib.sha256(b"- [ ] x \xe9").hexdigest()


# --- tick and untick --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "ticked"),
    [
        ("- [ ] Pay", "- [x] Pay ✅ 2026-10-29"),
        ("- [ ]", "- [x] ✅ 2026-10-29"),
        ("- [ ] #tag", "- [x] ✅ 2026-10-29 #tag"),
        ("- [ ]  ^b", "- [x]  ✅ 2026-10-29 ^b"),
        ("- [ ] Pay 📅 2026-10-22", "- [x] Pay 📅 2026-10-22 ✅ 2026-10-29"),
        ("- [ ] Pay #home ^b1", "- [x] Pay ✅ 2026-10-29 #home ^b1"),
        (
            "- [ ] Pay 📅 2026-10-22 #home ⏫ #t ^b1",
            "- [x] Pay 📅 2026-10-22 #home ⏫ ✅ 2026-10-29 #t ^b1",
        ),
        ("> - [/] Quoted ⏰ 19:00", "> - [x] Quoted ⏰ 19:00 ✅ 2026-10-29"),
        ("- [x] Done ✅ 2026-10-01 📅 2026-10-22", "- [x] Done ✅ 2026-10-29 📅 2026-10-22"),
    ],
)
def test_tick_inserts_the_done_date_after_the_last_field(line: str, ticked: str) -> None:
    task = one(line)
    new = task.tick(date(2026, 10, 29))
    assert new == ticked
    again = one(new)
    assert again.done == date(2026, 10, 29)
    assert again.status == "done"
    assert again.key == task.key
    assert (again.tags, again.block_id, again.due) == (task.tags, task.block_id, task.due)


def test_untick_removes_the_done_date() -> None:
    for line in ("- [x] Pay ✅ 2026-10-29", "- [x] Pay ✅ 2026-10-29 #home ^b"):
        task = one(line)
        assert one(task.untick()).done is None
        assert task.untick() == line.replace("[x]", "[ ]").replace(" ✅ 2026-10-29", "")
    assert one("- [x] ✅ 2026-10-29 📅 2026-10-22").untick() == "- [ ] 📅 2026-10-22"
    kept = one("- [x] Bad ✅ 2026-13-01")
    assert kept.untick() == "- [ ] Bad ✅ 2026-13-01"  # unknown text is never removed
    task = one("- [ ] Pay 📅 2026-10-22 #home")
    assert one(task.tick(date(2026, 10, 29))).untick() == task.render()


def test_tick_refuses_recurring_and_ambiguous_tasks() -> None:
    with pytest.raises(RecurringTaskError):
        one("- [ ] Water 🔁 every week").tick(date(2026, 10, 29))
    with pytest.raises(ValueError, match="more than one"):
        one("- [x] Two ✅ 2026-10-01 ✅ 2026-10-02").tick(date(2026, 10, 29))


def test_spans_for_edits() -> None:
    task = one("- [ ] Pay 📅 2026-10-22 #home ^b1")
    text = task.render()
    assert task.block_span is not None
    assert text[task.block_span[0] : task.block_span[1]] == "^b1"
    assert text[: task.fields_end].endswith("2026-10-22")
    assert task.body == "Pay 📅 2026-10-22 #home ^b1"
    assert one("- [ ] Pay #home").fields_end == len("- [ ] Pay")


# --- JSON projection --------------------------------------------------------------------


def test_the_json_projection_is_encodable_with_invalid_utf8() -> None:
    data = b"- [\xff] caf\xe9 #t\xe9g \xf0\x9f\x93\x85 2026-10-22 ^b\n- [ ] plain\n"
    document = parse(data, "d\udcffir/n.md")
    projected = document.to_json()
    text = json.dumps(projected, ensure_ascii=False)
    text.encode("utf-8")  # strict: no lone surrogate
    assert json.loads(text) == projected
    bad, plain = projected["tasks"]
    assert bad["invalid_utf8"] is True
    assert plain["invalid_utf8"] is False
    assert bad["description"] == "caf� #t�g"
    assert bad["status_char"] == "�"
    assert bad["due"] == "2026-10-22"
    assert projected["path"] == "d�ir/n.md"
    assert projected["invalid_utf8"] is True


def test_the_json_projection_of_every_field() -> None:
    task = one("- [ ] All ⏫ 🔁 every day ⏰ 2026-10-21 09:15 🆔 i1 ⛔ a,b 🏁 keep #t ^b")
    projected = task.to_json()
    assert projected["priority"] == "high"
    assert projected["reminder"] == {"date": "2026-10-21", "time": "09:15"}
    assert projected["depends_on"] == ["a", "b"]
    assert projected["on_completion"] == "keep"
    assert projected["key"] == "obsidian:id:i1"
    assert projected["fields"][0]["kind"] == "priority"
    assert json.loads(json.dumps(projected)) == projected


# --- context: headings, owned blocks, warnings ------------------------------------------


def test_headings_and_owned_blocks() -> None:
    note = (
        "- [ ] before\n# Inbox #\n- [ ] in inbox\n## Today\n"
        "<!-- horae:begin today -->\n- [ ] owned\n<!-- horae:end today -->\n- [ ] after\n"
        "<!-- horae:begin other -->\n<!-- horae:end today -->\n- [ ] still in other\n"
    )
    document = parse(note.encode())
    found = {t.description: (t.heading, t.owned_block, t.in_owned_block) for t in document.tasks}
    assert found == {
        "before": (None, None, False),
        "in inbox": ("Inbox", None, False),
        "owned": ("Today", "today", True),
        "after": ("Today", None, False),
        "still in other": ("Today", "other", True),
    }


def test_warnings() -> None:
    def kinds(text: str) -> list[tuple[str, int]]:
        return [(w.kind, w.line) for w in parse(text.encode()).warnings]

    assert kinds("---\ntitle: x\n") == [("unclosed_frontmatter", 1)]
    assert kinds("a\n```\n- [ ] x\n") == [("unclosed_fence", 2)]
    assert kinds("a\n%%\n- [ ] x\n") == [("unclosed_comment", 2)]
    assert kinds("- [ ] " + "a" * MAX_LINE_BYTES + "\n") == [("long_line", 1)]
    assert kinds("---\n---\n```\n```\n%%\n%%\n") == []


def test_inline_code_hides_a_comment_opener() -> None:
    assert [t.description for t in parse(b"Use `%%` here\n- [ ] seen\n").tasks] == ["seen"]
    assert parse(b"Use %% here\n- [ ] hidden\n").tasks == ()


def test_a_fence_indented_four_columns_is_indented_code() -> None:
    assert len(parse(b"    ```\n- [ ] not fenced\n").tasks) == 1
    nested = b"- item\n    ```\n    - [ ] fenced in the list item\n    ```\n- [ ] out\n"
    assert [t.description for t in parse(nested).tasks] == ["out"]
    closing = b"```\n    ```\n- [ ] still fenced\n```\n- [ ] out\n"
    assert [t.description for t in parse(closing).tasks] == ["out"]


def test_tags_are_ascii_digit_aware() -> None:
    assert one("- [ ] x #123 #١٢٣ #a1").tags == ("#١٢٣", "#a1")


def test_recurrence_accepts_a_trailing_comma() -> None:
    assert one("- [ ] x 🔁 every day,").recurrence == "every day,"


def test_reminder_times_stay_strict() -> None:
    assert one("- [ ] x ⏰ 09:00").reminder is not None
    for loose in ("⏰ 9:00", "⏰ 09:00:00"):
        task = one(f"- [ ] x {loose}")
        assert (task.reminder, task.description) == (None, f"x {loose}")
