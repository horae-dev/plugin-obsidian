# SPDX-License-Identifier: Apache-2.0
"""Hostile notes stay cheap: every scan is linear in the line's length, long lines and
lines past `MAX_LINES` are kept but not analysed, and memory follows the note's size."""

import sys
import time
import tracemalloc
from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from horae_obsidian.tasks import MAX_LINE_BYTES, MAX_LINES, Document, parse

MIB = 1024 * 1024
SLOWDOWN = 4 if "coverage" in sys.modules else 1
"""Line tracing (pytest --cov) slows the pure-Python loops down."""
TAIL = " x 🔁 a"

# Each unit once tripped a backtracking regex (masking, the 🔁 value, fields, tags).
UNITS = {
    "wikilink": "[[",
    "autolink": "<",
    "embed": "![[",
    "link": "[a](",
    "backticks": "` `` ``` ",
    "recurrence": "🔁 ",
    "spaces": " ",
    "letters": "a",
    "urls": "a:// ",
    "tags": "#a",
    "comments": "%%`",
    "carets": "^a ",
    "priorities": "⏫",
    "brackets": "[a]",
    "fields": "📅 2026-10-22 ",
}


def note(unit: str, total: int) -> bytes:
    """`total` bytes of task lines just under `MAX_LINE_BYTES`, each `unit` repeated."""
    text = "- [ ] " + unit * (MAX_LINE_BYTES // len(unit.encode()))
    raw = text.encode()[: MAX_LINE_BYTES - len(TAIL.encode())]
    line = raw.decode("utf-8", "ignore").encode() + TAIL.encode() + b"\n"
    assert len(line) <= MAX_LINE_BYTES + 1
    return line * (total // len(line))


def timed(data: bytes) -> tuple[float, Document]:
    started = time.perf_counter()
    document = parse(data, "x.md")
    return time.perf_counter() - started, document


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_a_mebibyte_of_hostile_lines_parses_fast(unit: str) -> None:
    data = note(UNITS[unit], MIB)
    elapsed, document = timed(data)
    assert elapsed < 1.0 * SLOWDOWN  # about 0.2 s at most on a laptop
    assert document.render() == data
    if unit != "comments":  # an odd `%%` count opens a comment block
        assert len(document.tasks) == data.count(b"\n")
        assert document.warnings == ()


@pytest.mark.parametrize("unit", ["wikilink", "backticks", "comments", "urls", "recurrence"])
def test_twice_the_size_takes_about_twice_the_time(unit: str) -> None:
    small = min(timed(note(UNITS[unit], MIB // 2))[0] for _ in range(3))
    large = min(timed(note(UNITS[unit], MIB))[0] for _ in range(3))
    assert large / small < 3.5  # linear: ~2; quadratic would be ~4


@pytest.mark.parametrize(
    "build",
    [
        lambda: b"- [ ] " + b"[[" * MIB,
        lambda: b"- [ ] " + "🔁".encode() + b" " * MIB + b"x#",
        lambda: b"- [ ] " + b"a" * MIB,
    ],
)
def test_a_line_past_the_cap_is_kept_but_not_analysed(build: Callable[[], bytes]) -> None:
    data = build() + b"\n- [ ] after\n"
    elapsed, document = timed(data)
    assert elapsed < 1.0 * SLOWDOWN
    assert document.render() == data
    assert [t.description for t in document.tasks] == ["after"]
    assert [(w.kind, w.line) for w in document.warnings] == [("long_line", 1)]
    assert document.lines[0].text.startswith("- [ ] ")


def test_the_cap_is_inclusive() -> None:
    line = b"- [ ] " + b"a" * (MAX_LINE_BYTES - 6)
    assert len(parse(line).tasks) == 1
    assert len(parse(line + b"a").tasks) == 0


def test_lines_past_max_lines_are_plain() -> None:
    data = b"\n" * MAX_LINES + b"- [ ] too far\n"
    document = parse(data)
    assert document.tasks == ()
    assert [(w.kind, w.line) for w in document.warnings] == [("too_many_lines", MAX_LINES + 1)]
    assert len(document.lines) == MAX_LINES + 1
    last = document.lines[-1]
    assert (last.number, last.text, last.eol) == (MAX_LINES + 1, "- [ ] too far", "\n")
    assert document.render() == data
    assert [line.number for line in document.lines[MAX_LINES - 1 :]] == [MAX_LINES, MAX_LINES + 1]


def test_a_newline_only_note_uses_little_memory() -> None:
    """4 MiB of line breaks: before line spans, 944 MiB peak RSS and 5.5 s."""
    data = b"\n" * (4 * MIB)
    elapsed, document = timed(data)
    assert elapsed < 2.0 * SLOWDOWN
    tracemalloc.start()
    try:
        document = parse(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(document.lines) == 4 * MIB
    assert peak < 8 * MIB  # the offsets of the first MAX_LINES lines


@settings(max_examples=50, deadline=1000)
@given(
    st.lists(st.sampled_from(sorted(UNITS.values())), min_size=1, max_size=8),
    st.integers(min_value=1, max_value=2000),
)
def test_random_hostile_mixtures_stay_fast(units: list[str], repeat: int) -> None:
    line = ("- [ ] " + "".join(units) * repeat)[:4000]
    data = (line + "\n").encode() * 4
    assert parse(data).render() == data
