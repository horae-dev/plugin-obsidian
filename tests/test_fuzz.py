# SPDX-License-Identifier: Apache-2.0
"""Properties (Hypothesis): parsing never raises and never loses a byte, a status edit
changes one character and no key, fields are ordered and disjoint, keys stay in the
fingerprint alphabet, the JSON projection always encodes, and an SDK-escaped title never
gains a field, a tag or an id."""

import json
import re
from datetime import date

from horae_sdk.tasks import escape_title, task_line
from hypothesis import given, settings
from hypothesis import strategies as st

from horae_obsidian.tasks import (
    MAX_FIELDS,
    SIGNIFIERS,
    VS16,
    Document,
    RecurringTaskError,
    Task,
    parse,
)

KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
STATUS = st.sampled_from([" ", "x", "X", "/", "-", ">", "?", "é", "🏡", "]"])

MARKDOWN = st.sampled_from(
    [
        *SIGNIFIERS,
        VS16,
        "- [ ] ",
        "- [x] ",
        "> ",
        "    ",
        "\t",
        "1. ",
        "* ",
        "[",
        "]",
        "#",
        "#tag",
        "^id",
        "%%",
        "```",
        "~~~",
        "---",
        "`",
        "[[",
        "]]",
        "::",
        " ",
        "\n",
        "\r\n",
        "\r",
        "2026-10-22",
        "2026-02-30",
        "19:00",
        "every day",
        "a",
        "é",
        chr(0xFEFF),  # a BOM in the middle
        chr(0x2028),  # LINE SEPARATOR, not a Markdown line break
        "\x00",
        "\r\r\n",  # a lone CR before a CRLF
        "<!-- horae:begin b -->",
        "<!-- horae:end b -->",
        "🆔 a1",
        "https://x.org/#t",
        "[a](b)",
        "<u>",
    ]
)
TEXT = st.lists(MARKDOWN, max_size=200).map("".join)
HOSTILE = st.lists(st.one_of(MARKDOWN, st.text(max_size=3)), max_size=40).map("".join)


def check_round_trip(data: bytes) -> None:
    document = parse(data, "fuzz.md")
    assert document.render() == data
    for line in document.lines:
        assert data[line.start : line.end] == line.text.encode("utf-8", "surrogateescape")
    json.dumps(document.to_json(), ensure_ascii=False).encode("utf-8")
    for task in document.tasks:
        assert document.replace_line(task, task.render()) == data
        last = task.body_offset
        for found in task.fields:
            assert task.line.text[found.start : found.end] == found.text
            assert last <= found.start < found.end  # ordered, disjoint, in the body
            last = found.end
        if task.block_span:
            assert task.block_span[0] >= last
        assert KEY.fullmatch(task.key)
        check_status_edits(document, task)


def check_status_edits(document: Document, task: Task) -> None:
    """A status edit changes only the status character, and no key."""
    for char in ("x", " ", "-"):
        edited = document.replace_line(task, task.with_status(char))
        assert len(edited) == len(document.data) + len(char.encode()) - len(
            task.status_char.encode("utf-8", "surrogateescape")
        )
        reparsed = parse(edited, document.path)
        assert [t.key for t in reparsed.tasks] == [t.key for t in document.tasks]
        if task.status_char.isascii():
            changed = [
                i for i, (a, b) in enumerate(zip(edited, document.data, strict=True)) if a != b
            ]
            prefix = len(task.line.text[: task.status_offset].encode("utf-8", "surrogateescape"))
            assert changed in ([], [task.line.start + prefix])
    try:
        ticked = task.tick(date(2026, 10, 29))
    except (RecurringTaskError, ValueError):
        return
    reparsed = parse(document.replace_line(task, ticked), document.path)
    assert [t.key for t in reparsed.tasks] == [t.key for t in document.tasks]
    again = next(t for t in reparsed.tasks if t.line.number == task.line.number)
    assert again.done == date(2026, 10, 29)
    assert parse(document.replace_line(task, again.untick()), document.path).tasks[0].key


@settings(max_examples=400)
@given(st.binary(max_size=2000))
def test_any_bytes_round_trip(data: bytes) -> None:
    check_round_trip(data)


@settings(max_examples=400)
@given(TEXT, st.sampled_from([b"", b"\xef\xbb\xbf"]), st.binary(max_size=4))
def test_markdown_like_text_round_trips(text: str, bom: bytes, junk: bytes) -> None:
    check_round_trip(bom + text.encode("utf-8") + junk)


@settings(max_examples=200)
@given(STATUS, TEXT.map(lambda t: t.replace("\n", " ").replace("\r", " ")))
def test_ticking_keeps_the_key(status: str, rest: str) -> None:
    document = parse(f"- [{status}] {rest}\n".encode(), "fuzz.md")
    for task in document.tasks:
        for new in (task.with_status("x"), task.with_status(" "), task.untick()):
            again = parse(document.replace_line(task, new), "fuzz.md")
            assert again.tasks[0].key == task.key
        try:
            ticked = task.tick(date(2026, 10, 29))
        except (RecurringTaskError, ValueError):
            continue
        ticked_task = parse(document.replace_line(task, ticked), "fuzz.md").tasks[0]
        assert (ticked_task.key, ticked_task.done, ticked_task.status) == (
            task.key,
            date(2026, 10, 29),
            "done",
        )


def test_at_most_max_fields_are_read() -> None:
    fields = " ⏫" * (MAX_FIELDS + 3)
    [task] = parse(f"- [ ] x{fields}".encode()).tasks
    assert len(task.fields) == MAX_FIELDS
    assert task.description == "x" + " ⏫" * 3


def test_a_cr_before_a_crlf_and_nul_bytes() -> None:
    data = b"- [ ] a\r\r\n- [x] b\x00 #t\x00g\n\x00"
    document = parse(data)
    assert [line.eol for line in document.lines] == ["\r", "\r\n", "\n", ""]
    assert [t.description for t in document.tasks] == ["a", "b\x00 #t\x00g"]
    assert document.render() == data


@settings(max_examples=400)
@given(HOSTILE)
def test_an_escaped_title_parses_back_inert(title: str) -> None:
    [task] = parse(task_line(title, due="2030-01-01").encode("utf-8", "surrogatepass")).tasks
    assert [f.kind for f in task.fields] == ["due"]
    assert task.description == escape_title(title)
    assert task.tags == ()
    assert task.block_id is None
