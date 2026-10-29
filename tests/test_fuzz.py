# SPDX-License-Identifier: Apache-2.0
"""Properties (Hypothesis): parsing never raises and never loses a byte, and an SDK-escaped
title never gains a field, a tag or an id."""

from horae_sdk.tasks import escape_title, task_line
from hypothesis import given, settings
from hypothesis import strategies as st

from horae_obsidian.tasks import SIGNIFIERS, VS16, parse

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
    ]
)
TEXT = st.lists(MARKDOWN, max_size=60).map("".join)
HOSTILE = st.lists(st.one_of(MARKDOWN, st.text(max_size=3)), max_size=40).map("".join)


def check_round_trip(data: bytes) -> None:
    document = parse(data, "fuzz.md")
    assert document.render() == data
    for line in document.lines:
        assert data[line.start : line.end] == line.text.encode("utf-8", "surrogateescape")
    for task in document.tasks:
        assert document.replace_line(task, task.render()) == data
        for found in task.fields:
            assert task.line.text[found.start : found.end] == found.text
        assert task.key


@settings(max_examples=400)
@given(st.binary(max_size=400))
def test_any_bytes_round_trip(data: bytes) -> None:
    check_round_trip(data)


@settings(max_examples=400)
@given(TEXT, st.sampled_from([b"", b"\xef\xbb\xbf"]), st.binary(max_size=4))
def test_markdown_like_text_round_trips(text: str, bom: bytes, junk: bytes) -> None:
    check_round_trip(bom + text.encode("utf-8") + junk)


@settings(max_examples=400)
@given(HOSTILE)
def test_an_escaped_title_parses_back_inert(title: str) -> None:
    [task] = parse(task_line(title, due="2030-01-01").encode("utf-8", "surrogatepass")).tasks
    assert [f.kind for f in task.fields] == ["due"]
    assert task.description == escape_title(title)
    assert task.tags == ()
    assert task.block_id is None
