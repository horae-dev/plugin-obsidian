# SPDX-License-Identifier: Apache-2.0
"""Read Obsidian Tasks lines from a Markdown note, keeping every byte.

This is the reading counterpart of `horae_sdk.tasks` (which escapes titles and writes
`task_line`s): it knows the same signifiers (`SIGNIFIERS`, each with an optional U+FE0F).

**Nothing is rewritten.** `parse(data)` keeps the note's bytes and indexes its lines by byte
span (`Line`: exact text, line ending, span, decoded on access); `Document.render()` gives back
the identical bytes, and `Document.replace_line(task, text)` changes only that line's span (the
status edit of `task.update_status`, see `Task.with_status`, `Task.tick`, `Task.untick`).
Bytes that are not UTF-8 are kept as lone surrogates (`surrogateescape`) and written back as
they were; a UTF-8 BOM is kept. `Task.to_json()` / `Document.to_json()` are the JSON-safe
projection (surrogates replaced, `invalid_utf8` flagged).

**What is a task** (the Tasks plugin's own rule): `[ \\t>]*`, a list marker `-` `*` `+` or
`1.` / `1)`, one or more spaces (not tabs), `[c]` with one status character, then the body
(`- [x]x` is a task, as in Tasks). Not in frontmatter, fenced code blocks (backticks or
tildes, also inside blockquotes and lists; a fence indented 4 columns or more past its
container is indented code, not a fence) or `%%` comment blocks (a `%%` inside inline code
does not count). Blockquotes and callouts (`> - [ ]`) and nested lists are tasks too.

**Fields** are read the Tasks way: from the **end** of the line backwards, as long as the
end is a field (`📅 2026-10-22`, `⏰ 19:00`, `🔁 every day`, a priority…), a trailing tag or,
first, a block id (`^abc`). The text before them is the description; a signifier in the
middle of the text is text. A field whose value does not parse (`📅 2026-02-30`) is kept as
**unknown** (`Field.valid` false, in `Task.unknown`), never interpreted. The ⏰ reminder is
Horae's `⏰ HH:MM` (the time on the due date, `horae_sdk.tasks.task_line`); the Reminder
plugin's `⏰ YYYY-MM-DD HH:MM` and `⏰ YYYY-MM-DD` are read too. Not supported yet: the Tasks
global filter and Dataview inline fields (`[due:: …]` stays text).

**Ids.** `Task.key` is a stable fingerprint (`horae_sdk.intents.fingerprint`, the
`task.update_status` alphabet): the Tasks `🆔` id when there is one (vault-wide), else the
block id (per note), else a digest of the note path and the description (both NFC, runs of
spaces and tabs as one space), with its occurrence among identical descriptions of the note
(every task counts, whatever its key). Keys that collide in a note are flagged
(`Task.key_unique` false, `Document.duplicate_keys`); `vault.scan` does the same across the
vault. The README's "Task identity" section lists which edits change a key.

**Bounds** (vault text is untrusted, threat model T02): every scan is linear in the line's
length; a line longer than `MAX_LINE_BYTES` and every line after the first `MAX_LINES` are
kept byte-exact but not analysed (`Document.warnings`). The parser only reads; a description
is never fed back to a Tasks line without `horae_sdk.tasks.escape_title`.
"""

import hashlib
import re
import unicodedata
from array import array
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal, overload

from horae_sdk.intents import fingerprint

NAME = "obsidian"
VS16 = chr(0xFE0F)  # the emoji variation selector
BOM = b"\xef\xbb\xbf"
ENCODING_ERRORS = "surrogateescape"
"""How bytes that are not UTF-8 are decoded (and encoded back): lone surrogates."""

Kind = Literal[
    "due",
    "scheduled",
    "start",
    "created",
    "done",
    "cancelled",
    "recurrence",
    "reminder",
    "priority",
    "id",
    "depends_on",
    "on_completion",
]
Status = Literal["todo", "done", "in_progress", "cancelled", "custom"]
Priority = Literal["highest", "high", "medium", "low", "lowest"]
WarningKind = Literal[
    "unclosed_fence", "unclosed_comment", "unclosed_frontmatter", "long_line", "too_many_lines"
]

DATE_SIGNIFIERS: dict[str, Kind] = {
    "\U0001f4c5": "due",  # 📅
    "\U0001f4c6": "due",  # 📆
    "\U0001f5d3": "due",  # 🗓
    "⏳": "scheduled",
    "⌛": "scheduled",
    "\U0001f6eb": "start",  # 🛫
    "➕": "created",  # noqa: RUF001 (the Tasks signifier, not a plus sign)
    "✅": "done",
    "❌": "cancelled",
}
PRIORITIES: dict[str, Priority] = {
    "\U0001f53a": "highest",  # 🔺
    "⏫": "high",
    "\U0001f53c": "medium",  # 🔼
    "\U0001f53d": "low",  # 🔽
    "⏬": "lowest",
}
RECURRENCE = "\U0001f501"  # 🔁
REMINDER = "⏰"
ID = "\U0001f194"  # 🆔
DEPENDS_ON = "⛔"
ON_COMPLETION = "\U0001f3c1"  # 🏁
DONE = "✅"
SIGNIFIERS: dict[str, Kind] = {
    **DATE_SIGNIFIERS,
    RECURRENCE: "recurrence",
    REMINDER: "reminder",
    **dict.fromkeys(PRIORITIES, "priority"),
    ID: "id",
    DEPENDS_ON: "depends_on",
    ON_COMPLETION: "on_completion",
}
"""Every signifier read, with the field it starts (the same set as `horae_sdk.tasks`)."""
_SIGNIFIER_TEXT = {text: text for sig in SIGNIFIERS for text in (sig, sig + VS16)}
"""One shared string per signifier spelling (a note can hold 100k of them)."""

STATUSES: dict[str, Status] = {
    " ": "todo",
    "x": "done",
    "X": "done",
    "/": "in_progress",
    "-": "cancelled",
}
"""Status characters with a meaning; any other (`[>]`, `[?]`…) is `custom`, kept as is
(whether a custom status counts as open or closed is the source's call, #4)."""

MAX_FIELDS = 64
"""At most this many trailing fields and tags are read per line (the rest is text)."""
MAX_LINE_BYTES = 16 * 1024
"""A longer line is kept byte-exact as a plain line, never a task (`long_line`)."""
MAX_LINES = 100_000
"""Lines past this many are kept byte-exact as plain lines (`too_many_lines`)."""
MAX_ID = 128
"""A longer 🆔, ⛔ or `^block-id` value is text, not an id."""

_BREAK = re.compile(rb"\r\n|\r|\n")
_LINE_END = re.compile(r"\r\n|\r|\n")
_TASK = re.compile(
    r"(?P<prefix>[ \t>]*)(?P<marker>[-*+]|[0-9]{1,9}[.)]) +"
    r"\[(?P<status>[^\r\n])\]\s*(?P<body>.*)",
    re.DOTALL,
)
_LIST_ITEM = re.compile(r"(?P<prefix>[ \t>]*)(?P<marker>[-*+]|[0-9]{1,9}[.)])(?P<space>[ \t]*)")
_HEADING = re.compile(r" {0,3}(?P<level>#{1,6})(?:[ \t]|$)")
_OWNED_MARKER = re.compile(r"[ \t]*<!-- horae:(?P<edge>begin|end) (?P<block>[a-z0-9-]+) -->[ \t]*")
_FENCE = re.compile(r"(?P<lead>[ \t>]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)", re.DOTALL)
_FRONTMATTER_OPEN = re.compile(r"---[ \t]*")
_FRONTMATTER_CLOSE = re.compile(r"(?:---|\.\.\.)[ \t]*")
_COMMENT = "%%"

_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_TIME = r"[0-9]{2}:[0-9]{2}"
_ID_CHARS = r"[A-Za-z0-9_-]+"
_SHAPES: dict[Kind, re.Pattern[str]] = {
    **{kind: re.compile(_DATE) for kind in DATE_SIGNIFIERS.values()},
    "reminder": re.compile(rf"{_DATE}(?: +{_TIME})?|{_TIME}"),
    "recurrence": re.compile(r"[a-zA-Z0-9, !]+"),  # Tasks: a trailing `,` or space is fine
    "priority": re.compile(""),
    "id": re.compile(_ID_CHARS),
    "depends_on": re.compile(rf"{_ID_CHARS}(?: *, *{_ID_CHARS})*"),
    "on_completion": re.compile(r"[a-zA-Z]+"),
}
"""The value shape of each field (after the spaces that follow the signifier)."""
_TAG_CHARS = r"[^\s!@#$%^&*(),.?\":{}|<>\[\]`'\\;=+~]"
_TAG = re.compile(rf"(?<![^\s])#(?P<tag>{_TAG_CHARS}+)")
_DIGITS = re.compile(r"[0-9]+")
# Reversed-string patterns: the line is read from its end with `match`, never `search`.
_REV_SPACE = re.compile(r"\s*")
_REV_VALUE = re.compile(r"[A-Za-z0-9 ,!:_\-]*")  # every character a field value can hold
_REV_TAG = re.compile(rf"{_TAG_CHARS}+")
_REV_BLOCK = re.compile(r"[A-Za-z0-9-]+")

_MASK = "`"
"""What masked text becomes: neither whitespace nor a tag character."""


def _real_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _real_time(text: str) -> bool:
    hours, minutes = text.split(":")
    return int(hours) < 24 and int(minutes) < 60


def _has_surrogate(text: str) -> bool:
    return any("\ud800" <= c <= "\udfff" for c in text) if not text.isascii() else False


def json_safe(text: str) -> str:
    """`text` with every lone surrogate (an invalid UTF-8 byte) replaced by U+FFFD."""
    if not _has_surrogate(text):
        return text
    try:
        raw = text.encode("utf-8", ENCODING_ERRORS)
    except UnicodeEncodeError:  # a surrogate that is not an escaped byte
        raw = text.encode("utf-8", "surrogatepass")
    return raw.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class ParseWarning:
    """Something the parser did not analyse or could not close (the bytes are kept)."""

    kind: WarningKind
    line: int
    """1-based line number where it starts."""


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a note, exactly as it is in the file."""

    number: int
    """1-based."""
    text: str
    """The line without its ending (non-UTF-8 bytes as lone surrogates)."""
    eol: str
    """`\\n`, `\\r\\n`, `\\r`, or `""` for a last line with no ending."""
    start: int
    """Byte offset of the line in the file (after a BOM)."""
    end: int
    """Byte offset where the line's text ends (its ending excluded)."""

    @property
    def invalid_utf8(self) -> bool:
        return _has_surrogate(self.text)


class Lines(Sequence[Line]):
    """Every line of a note, decoded on access: only the byte offsets of the first
    `MAX_LINES` lines are stored (an `array`), later ones are found by scanning."""

    def __init__(self, data: bytes, base: int) -> None:
        self._data = data
        tail = len(data) - base
        breaks = data.count(b"\n", base) + data.count(b"\r", base) - data.count(b"\r\n", base)
        self._count = breaks if breaks and data.endswith((b"\n", b"\r")) else breaks + 1
        if tail == 0:
            self._count = 1
        indexed = min(self._count, MAX_LINES)
        bounds = array("Q", [base])
        if indexed > 1 or self._count > indexed:
            for match in _BREAK.finditer(data, base):
                bounds.append(match.end())
                if len(bounds) > indexed:
                    break
        if len(bounds) == indexed:  # the last indexed line is the last line
            bounds.append(len(data))
        self._bounds = bounds
        self._indexed = indexed

    def __len__(self) -> int:
        return self._count

    @property
    def indexed(self) -> int:
        """How many lines are indexed (and analysed): at most `MAX_LINES`."""
        return self._indexed

    def _make(self, index: int, start: int, stop: int) -> Line:
        data = self._data
        if data[stop - 2 : stop] == b"\r\n" and stop - 2 >= start:
            eol = "\r\n"
        elif stop > start and data[stop - 1] in (0x0A, 0x0D):
            eol = chr(data[stop - 1])
        else:
            eol = ""
        end = stop - len(eol)
        return Line(index + 1, data[start:end].decode("utf-8", ENCODING_ERRORS), eol, start, end)

    def span(self, index: int) -> tuple[int, int]:
        """The byte span (`start`, `end` of the line and its ending) of an indexed line."""
        return self._bounds[index], self._bounds[index + 1]

    def too_long(self, index: int) -> bool:
        """Whether indexed line `index` has more than `MAX_LINE_BYTES` (its ending aside)."""
        start, stop = self.span(index)
        if stop - start <= MAX_LINE_BYTES:
            return False
        if stop - start > MAX_LINE_BYTES + 2:
            return True
        line = self._make(index, start, stop)
        return line.end - line.start > MAX_LINE_BYTES

    def _beyond(self, start_index: int) -> Iterator[Line]:
        """The lines from `start_index` (past the indexed ones) to the end, by scanning."""
        index, start = self._indexed, self._bounds[self._indexed]
        for match in _BREAK.finditer(self._data, start):
            if index >= start_index:
                yield self._make(index, start, match.end())
            index, start = index + 1, match.end()
        if start < len(self._data):
            yield self._make(index, start, len(self._data))

    @overload
    def __getitem__(self, index: int) -> Line: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[Line]: ...
    def __getitem__(self, index: int | slice) -> Line | Sequence[Line]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError("line index out of range")
        if index < self._indexed:
            return self._make(index, *self.span(index))
        return next(self._beyond(index))

    def __iter__(self) -> Iterator[Line]:
        for index in range(self._indexed):
            yield self._make(index, *self.span(index))
        if self._count > self._indexed:
            yield from self._beyond(self._indexed)


@dataclass(frozen=True, slots=True)
class Field:
    """A trailing field of a task line, at `start:end` (character offsets in the line)."""

    kind: Kind
    signifier: str
    """As written: the emoji and its U+FE0F if any."""
    value: str
    """The raw value text (`""` for a priority)."""
    gap: int
    """How many spaces separate the signifier from the value."""
    start: int
    end: int
    valid: bool
    """False when the value does not parse (a date that does not exist, `⏰ 25:00`)."""

    @property
    def text(self) -> str:
        """The field's exact text in the line (`line.text[start:end]`)."""
        return self.signifier + " " * self.gap + self.value

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "signifier": self.signifier,
            "value": json_safe(self.value),
            "text": json_safe(self.text),
            "start": self.start,
            "end": self.end,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class Reminder:
    """`⏰ HH:MM` (on the due date), `⏰ YYYY-MM-DD HH:MM` or `⏰ YYYY-MM-DD`."""

    date: date | None
    time: str | None


class RecurringTaskError(ValueError):
    """`Task.tick` on a recurring task: what completing one means is decided in #4."""


@dataclass(frozen=True, slots=True)
class Task:
    """A task line. `line.text` is the exact original text: render it, never rebuild it."""

    line: Line
    path: str
    """The note path as given (raw, for I/O); keys use its NFC form."""
    prefix: str
    """Indentation and blockquote markers before the list marker (`"  "`, `"> "`)."""
    marker: str
    """`-`, `*`, `+`, or `1.` / `1)`."""
    status_char: str
    status_offset: int
    """Character offset of the status character in `line.text`."""
    body_offset: int
    """Character offset of the body (after `]` and its spaces) in `line.text`."""
    description: str
    """The body without its trailing fields and block id, stripped (trailing tags kept)."""
    fields: tuple[Field, ...]
    """Trailing fields, left to right, invalid ones included."""
    fields_end: int
    """Character offset where a new field goes: the end of the last field, else the end of
    the description's text (before its trailing tags). Trailing tags and the block id come
    after it."""
    tags: tuple[str, ...]
    """`#tags` of the body (not in code, links, URLs or comments), in order, with `#`."""
    block_id: str | None
    block_span: tuple[int, int] | None
    """Character offsets of `^block-id` in `line.text`."""
    depth: int
    """Enclosing list items (0: top level)."""
    parent: int | None
    """Line number of the enclosing list item, if any."""
    heading: str | None
    """The text of the nearest ATX heading above the task, if any."""
    owned_block: str | None
    """The Horae owned block (`<!-- horae:begin <name> -->` … `end`) the task is in."""
    key: str
    """Stable fingerprint (see the module docstring)."""
    key_unique: bool = True
    """False when another task of the note (or, after `vault.scan`, of the vault) has the
    same key: a source must not act on it (#4 withholds and reports it)."""

    @property
    def body(self) -> str:
        """Everything after `]` and its spaces."""
        return self.line.text[self.body_offset :]

    @property
    def status(self) -> Status:
        return STATUSES.get(self.status_char, "custom")

    @property
    def in_owned_block(self) -> bool:
        return self.owned_block is not None

    @property
    def invalid_utf8(self) -> bool:
        return self.line.invalid_utf8

    @property
    def line_sha256(self) -> str:
        """SHA-256 of the line's exact bytes (its ending excluded): what #4 compares before
        it replaces the line."""
        raw = self.line.text.encode("utf-8", ENCODING_ERRORS)
        return hashlib.sha256(raw).hexdigest()

    def _effective(self, kind: Kind) -> Field | None:
        # Tasks reads from the end: the leftmost valid field of a kind wins.
        return next((f for f in self.fields if f.kind == kind and f.valid), None)

    def _date(self, kind: Kind) -> date | None:
        found = self._effective(kind)
        return date.fromisoformat(found.value) if found else None

    @property
    def due(self) -> date | None:
        return self._date("due")

    @property
    def scheduled(self) -> date | None:
        return self._date("scheduled")

    @property
    def start(self) -> date | None:
        return self._date("start")

    @property
    def created(self) -> date | None:
        return self._date("created")

    @property
    def done(self) -> date | None:
        return self._date("done")

    @property
    def cancelled(self) -> date | None:
        return self._date("cancelled")

    @property
    def reminder(self) -> Reminder | None:
        found = self._effective("reminder")
        if found is None:
            return None
        parts = found.value.split()
        if ":" in parts[-1]:
            day = date.fromisoformat(parts[0]) if len(parts) == 2 else None
            return Reminder(day, parts[-1])
        return Reminder(date.fromisoformat(parts[0]), None)

    @property
    def recurrence(self) -> str | None:
        found = self._effective("recurrence")
        return found.value if found else None

    @property
    def priority(self) -> Priority | None:
        found = self._effective("priority")
        return PRIORITIES[found.signifier[0]] if found else None

    @property
    def id(self) -> str | None:
        found = self._effective("id")
        return found.value if found else None

    @property
    def depends_on(self) -> tuple[str, ...]:
        found = self._effective("depends_on")
        return tuple(p.strip() for p in found.value.split(",")) if found else ()

    @property
    def on_completion(self) -> str | None:
        found = self._effective("on_completion")
        return found.value if found else None

    @property
    def unknown(self) -> tuple[str, ...]:
        """The text of the fields whose value does not parse, kept, never interpreted."""
        return tuple(f.text for f in self.fields if not f.valid)

    def render(self) -> str:
        """The line's text, unchanged (a parsed task is never rebuilt from its fields)."""
        return self.line.text

    def with_status(self, char: str) -> str:
        """The line with only its status character replaced (`x`, ` `, `-`, `/`…): one
        printable code point (not a line break, a control character or a surrogate)."""
        if len(char) != 1 or not char.isprintable():
            raise ValueError("a status is one printable character")
        text, at = self.line.text, self.status_offset
        return text[:at] + char + text[at + 1 :]

    def tick(self, done_on: date) -> str:
        """The line marked done on `done_on`: status `x` and ` ✅ YYYY-MM-DD` after the last
        field (before trailing tags and the block id); an existing valid ✅ date is replaced.
        `RecurringTaskError` on a recurring task (#4 decides what completing one means)."""
        if self.recurrence is not None:
            raise RecurringTaskError("completing a recurring task is not supported yet")
        stamp = f"{DONE} {done_on.isoformat()}"
        text = self.with_status("x")
        done = [f for f in self.fields if f.kind == "done" and f.valid]
        if len(done) > 1:
            raise ValueError("the task has more than one ✅ date")
        if done:
            return text[: done[0].start] + stamp + text[done[0].end :]
        at = self.fields_end
        lead = "" if at and text[at - 1] in " \t" else " "
        trail = " " if at < len(text) and not text[at].isspace() else ""
        return text[:at] + lead + stamp + trail + text[at:]

    def untick(self) -> str:
        """The line marked to do: status ` ` and every valid ✅ date removed (with the spaces
        before it). Unknown fields are kept."""
        text = self.with_status(" ")
        floor = self.status_offset + 2
        for found in reversed(self.fields):
            if found.kind != "done" or not found.valid:
                continue
            start = found.start
            while start > floor and text[start - 1] in " \t":
                start -= 1
            text = text[:start] + text[found.end :]
        return text

    def to_json(self) -> dict[str, Any]:
        """A JSON-safe projection (surrogates replaced, `invalid_utf8` flagged)."""
        reminder = self.reminder

        def day(value: date | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "key": self.key,
            "key_unique": self.key_unique,
            "path": json_safe(self.path),
            "line": self.line.number,
            "line_sha256": self.line_sha256,
            "invalid_utf8": self.invalid_utf8,
            "status": self.status,
            "status_char": json_safe(self.status_char),
            "description": json_safe(self.description),
            "tags": [json_safe(t) for t in self.tags],
            "fields": [f.to_json() for f in self.fields],
            "unknown": [json_safe(t) for t in self.unknown],
            "due": day(self.due),
            "scheduled": day(self.scheduled),
            "start": day(self.start),
            "created": day(self.created),
            "done": day(self.done),
            "cancelled": day(self.cancelled),
            "reminder": ({"date": day(reminder.date), "time": reminder.time} if reminder else None),
            "recurrence": self.recurrence,
            "priority": self.priority,
            "id": self.id,
            "depends_on": list(self.depends_on),
            "on_completion": self.on_completion,
            "block_id": self.block_id,
            "depth": self.depth,
            "parent": self.parent,
            "heading": json_safe(self.heading) if self.heading is not None else None,
            "owned_block": self.owned_block,
        }


@dataclass(frozen=True)
class Document:
    """A parsed note: its bytes, its lines and its tasks."""

    path: str
    data: bytes = field(repr=False)
    bom: bool
    lines: Lines = field(repr=False, compare=False)
    tasks: tuple[Task, ...] = field(repr=False)
    invalid_utf8: bool
    warnings: tuple[ParseWarning, ...] = ()
    duplicate_keys: tuple[str, ...] = ()
    """Keys held by more than one task of the note (those tasks are not `key_unique`)."""
    sha256: str = ""
    """SHA-256 of `data`: with `size`, `mtime_ns` and `ino`, what #4 compares before it
    writes the note back (compare-and-swap)."""
    size: int = 0
    mtime_ns: int | None = None
    ino: int | None = None

    def render(self) -> bytes:
        """The note's bytes, rebuilt from its lines: always `data`."""
        out = bytearray(BOM if self.bom else b"")
        for line in self.lines:
            out += (line.text + line.eol).encode("utf-8", ENCODING_ERRORS)
        return bytes(out)

    def replace_line(self, task: Task, text: str) -> bytes:
        """The note's bytes with only `task`'s line text replaced (its ending kept)."""
        if _LINE_END.search(text):
            raise ValueError("a line cannot hold a line break")
        line = task.line
        if line.number > len(self.lines) or self.lines[line.number - 1] != line:
            raise ValueError("the task is not a line of this document")
        new = text.encode("utf-8", ENCODING_ERRORS)
        return self.data[: line.start] + new + self.data[line.end :]

    def to_json(self) -> dict[str, Any]:
        """A JSON-safe projection of the note and its tasks."""
        return {
            "path": json_safe(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "mtime_ns": self.mtime_ns,
            "ino": self.ino,
            "bom": self.bom,
            "invalid_utf8": self.invalid_utf8,
            "warnings": [{"kind": w.kind, "line": w.line} for w in self.warnings],
            "duplicate_keys": list(self.duplicate_keys),
            "tasks": [task.to_json() for task in self.tasks],
        }


# --- inline code, comments, links and URLs: linear passes ------------------------------


class _Ticks:
    """Backtick runs, and the closing run of each opening one (CommonMark: the next run of
    exactly the same length), for openers taken left to right (amortised linear)."""

    def __init__(self, text: str) -> None:
        self.runs = [(m.start(), m.end()) for m in re.finditer("`+", text)]
        self.by_length: dict[int, list[int]] = {}
        for index, (start, end) in enumerate(self.runs):
            self.by_length.setdefault(end - start, []).append(index)
        self._pointer: dict[int, int] = dict.fromkeys(self.by_length, 0)

    def closer(self, index: int) -> int:
        """The index of the run closing run `index`, -1 if none."""
        start, end = self.runs[index]
        same = self.by_length[end - start]
        pointer = self._pointer[end - start]
        while pointer < len(same) and same[pointer] <= index:
            pointer += 1
        self._pointer[end - start] = pointer
        return same[pointer] if pointer < len(same) else -1


def _code_spans(text: str) -> list[tuple[int, int]]:
    """The inline code spans of `text`, left to right."""
    spans: list[tuple[int, int]] = []
    if "`" not in text:
        return spans
    ticks = _Ticks(text)
    if len(ticks.by_length) == 1:  # one run length (the usual case): consecutive pairs
        runs = ticks.runs
        return [(runs[i][0], runs[i + 1][1]) for i in range(0, len(runs) - 1, 2)]
    index = 0
    while index < len(ticks.runs):
        close = ticks.closer(index)
        if close < 0:
            index += 1
            continue
        spans.append((ticks.runs[index][0], ticks.runs[close][1]))
        index = close + 1
    return spans


# Each pattern is linear: an attempt scans at most to the next delimiter, and a lazy
# pattern only runs up to the last closing delimiter (`_paired`).
_INLINE_COMMENT = re.compile(r"%%.*?%%", re.DOTALL)
_WIKILINK = re.compile(r"!?\[\[.*?\]\]", re.DOTALL)
_LINK = re.compile(r"!?\[[^\[\]]*\]\([^()]*\)")
_AUTOLINK = re.compile(r"<[^<>\s]*>")
_URL = re.compile(r"(?<![A-Za-z0-9+.\-])[0-9+.\-]*[A-Za-z][A-Za-z0-9+.\-]*://\S*")


def _blank(match: re.Match[str]) -> str:
    return _MASK * (match.end() - match.start())


def _paired(pattern: re.Pattern[str], close: str, text: str) -> str:
    """`pattern` (lazy, ending with `close`) masked, searched only up to the last `close`."""
    last = text.rfind(close)
    if last < 0:
        return text
    cut = last + len(close)
    return pattern.sub(_blank, text[:cut]) + text[cut:]


def _masked(body: str) -> str:
    """`body` with inline code, then `%%` comments, wikilinks and embeds, Markdown links
    and images, autolinks and bare URLs masked (same length): each pass is linear."""
    text = body
    if "`" in text:
        parts: list[str] = []
        last = 0
        for start, end in _code_spans(text):
            parts += [text[last:start], _MASK * (end - start)]
            last = end
        text = "".join([*parts, text[last:]])
    if _COMMENT in text:
        text = _paired(_INLINE_COMMENT, _COMMENT, text)
    if "[" in text:
        text = _paired(_WIKILINK, "]]", text)
        text = _LINK.sub(_blank, text)
    if "<" in text:
        text = _AUTOLINK.sub(_blank, text)
    if "://" in text:
        text = _URL.sub(_blank, text)
    return text


def _tags(body: str) -> tuple[str, ...]:
    found = (m.group("tag") for m in _TAG.finditer(_masked(body)))
    return tuple(f"#{tag}" for tag in found if not _DIGITS.fullmatch(tag))


def _valid(kind: Kind, value: str) -> bool:
    if kind in DATE_SIGNIFIERS.values():
        return _real_date(value) is not None
    if kind == "reminder":
        parts = value.split()
        return all(_real_time(p) if ":" in p else _real_date(p) is not None for p in parts)
    return True


def _shape(kind: Kind, value: str) -> bool:
    if not _SHAPES[kind].fullmatch(value):
        return False
    if kind in ("id", "depends_on"):
        return all(len(part.strip()) <= MAX_ID for part in value.split(","))
    return True


@dataclass(frozen=True)
class _Trailer:
    description: str
    fields: list[Field]
    fields_end: int
    block_id: str | None
    block_span: tuple[int, int] | None


def _fields(body: str, offset: int, before: str) -> _Trailer:
    """The description, trailing fields (left to right) and block id of `body`, which
    starts at character `offset` of the line, after `before`. Read from the end with
    anchored matches on the reversed body: linear in the body's length."""
    n = len(body)
    reverse = body[::-1]

    def back(pattern: re.Pattern[str], end: int) -> int:
        """How many characters before `end` match `pattern` (read backwards)."""
        match = pattern.match(reverse, n - end)
        return match.end() - (n - end) if match else 0

    end = n - back(_REV_SPACE, n)
    block_id, block_span = None, None
    length = back(_REV_BLOCK, end)
    caret = end - length - 1
    if (
        0 < length <= MAX_ID
        and caret >= 0
        and body[caret] == "^"
        and (body[caret - 1] if caret else before) in (" ", "\t")
    ):
        block_id, block_span = body[caret + 1 : end], (offset + caret, offset + end)
        end = caret - back(_REV_SPACE, caret)
    fields: list[Field] = []
    trailing_tags: list[str] = []
    for _ in range(MAX_FIELDS):
        length = back(_REV_TAG, end)
        hash_at = end - length - 1
        if (
            length
            and hash_at >= 0
            and body[hash_at] == "#"
            and (hash_at == 0 or body[hash_at - 1].isspace())
            and not _DIGITS.fullmatch(body[hash_at + 1 : end])
        ):
            trailing_tags.insert(0, body[hash_at:end])
            end = hash_at - back(_REV_SPACE, hash_at)
            continue
        value_at = end - back(_REV_VALUE, end)
        sig_at = value_at - 1
        if sig_at >= 1 and body[sig_at] == VS16:
            sig_at -= 1
        kind = SIGNIFIERS.get(body[sig_at]) if sig_at >= 0 else None
        if kind is None:
            break
        tail = body[value_at:end]
        value = tail.lstrip(" ")
        if not _shape(kind, value):
            break
        fields.insert(
            0,
            Field(
                kind=kind,
                signifier=_SIGNIFIER_TEXT.get(body[sig_at:value_at], body[sig_at:value_at]),
                value=value,
                gap=len(tail) - len(value),
                start=offset + sig_at,
                end=offset + end,
                valid=_valid(kind, value),
            ),
        )
        end = sig_at - back(_REV_SPACE, sig_at)
    text_end = end
    description = " ".join([body[:text_end].strip(), *trailing_tags]).strip()
    fields_end = fields[-1].end if fields else offset + text_end
    return _Trailer(description, fields, fields_end, block_id, block_span)


def _indent_width(prefix: str) -> tuple[int, int]:
    """(blockquote depth, indentation width after the last `>`, a tab as 4)."""
    quotes = prefix.count(">")
    after = prefix.rsplit(">", 1)[-1]
    if quotes:
        after = after[1:] if after.startswith(" ") else after
    return quotes, sum(4 if c == "\t" else 1 for c in after)


def _heading_text(text: str, level_end: int) -> str:
    rest = text[level_end:].strip(" \t")
    closed = rest.rstrip("#")
    if not closed or closed[-1] in " \t":
        rest = closed.rstrip(" \t")
    return rest


def _comment_toggles(text: str) -> bool:
    """Whether `text` holds an odd number of `%%` outside inline code."""
    if _COMMENT not in text:
        return False
    spans = _code_spans(text) if "`" in text else []
    count, span = 0, 0
    for match in re.finditer(re.escape(_COMMENT), text):
        while span < len(spans) and spans[span][1] <= match.start():
            span += 1
        if span < len(spans) and spans[span][0] <= match.start():
            continue
        count += 1
    return count % 2 == 1


def _normal(text: str) -> str:
    """NFC, runs of spaces and tabs as one space, trimmed: what a digest key hashes."""
    return re.sub(r"[ \t]+", " ", unicodedata.normalize("NFC", text)).strip(" \t")


def _digest(description: str) -> str:
    normal = _normal(description).encode("utf-8", ENCODING_ERRORS)
    return hashlib.sha256(normal).hexdigest()[:16]


def _key(path: str, ids: list[str], block_id: str | None, digest: str, occurrence: int) -> str:
    """The task's fingerprint; a part `fingerprint` refuses falls back to the digest key."""
    for parts in (
        ("id", ids[0]) if ids else None,
        ("block", path, block_id) if block_id is not None else None,
        ("task", path, digest, str(occurrence)),
    ):
        if parts is None:
            continue
        try:
            return fingerprint(NAME, *parts)
        except Exception:  # noqa: S112 (any refusal falls back to the next key)
            continue
    raw = "\0".join([path, digest, str(occurrence)]).encode("utf-8", "surrogatepass")
    return f"{NAME}:task::{hashlib.sha256(raw).hexdigest()[:16]}"


@dataclass
class _State:
    """The block structure above the current line."""

    fence: str | None = None
    fence_line: int = 0
    fence_base: int = 0
    comment_line: int = 0
    comment: bool = False
    heading: str | None = None
    owned: str | None = None
    stack: list[tuple[int, int, int, int]] = field(default_factory=list)
    """Open list items: (quote depth, indent width, line number, content column)."""

    def base(self, quotes: int, width: int) -> int:
        """The content column of the innermost list item `width` belongs to (0: none)."""
        for item_quotes, _, _, content in reversed(self.stack):
            if item_quotes == quotes and content <= width:
                return content
        return 0


def _frontmatter_end(lines: Lines) -> int | None:
    """The index of the first content line after the frontmatter, `None` if unclosed."""
    for index in range(1, lines.indexed):
        if lines.too_long(index):
            continue
        if _FRONTMATTER_CLOSE.fullmatch(lines[index].text):
            return index + 1
    return None


def parse(
    data: bytes, path: str = "", *, mtime_ns: int | None = None, ino: int | None = None
) -> Document:
    """`data` (a note's bytes) parsed: never raises, never loses a byte (`render()`)."""
    bom = data.startswith(BOM)
    base = len(BOM) if bom else 0
    lines = Lines(data, base)
    invalid = False
    if not data.isascii():
        try:
            data[base:].decode("utf-8")
        except UnicodeDecodeError:
            invalid = True
    warnings: list[ParseWarning] = []
    key_path = unicodedata.normalize("NFC", path) or "-"
    tasks: list[Task] = []
    seen: dict[str, int] = {}
    state = _State()
    index = 0
    if lines.indexed and _FRONTMATTER_OPEN.fullmatch(lines[0].text):
        end = _frontmatter_end(lines)
        if end is None:
            warnings.append(ParseWarning("unclosed_frontmatter", 1))
        else:
            index = end
    for number in range(index, lines.indexed):
        if lines.too_long(number):
            warnings.append(ParseWarning("long_line", number + 1))
            continue
        line = lines[number]
        task = _analyse(line, state, path, key_path, seen)
        if task is not None:
            tasks.append(task)
    if state.fence is not None:
        warnings.append(ParseWarning("unclosed_fence", state.fence_line))
    if state.comment:
        warnings.append(ParseWarning("unclosed_comment", state.comment_line))
    if len(lines) > lines.indexed:
        warnings.append(ParseWarning("too_many_lines", lines.indexed + 1))
    counts = Counter(task.key for task in tasks)
    duplicates = tuple(sorted(k for k, c in counts.items() if c > 1))
    if duplicates:
        tasks = [replace(t, key_unique=False) if counts[t.key] > 1 else t for t in tasks]
    return Document(
        path=path,
        data=data,
        bom=bom,
        lines=lines,
        tasks=tuple(tasks),
        invalid_utf8=invalid,
        warnings=tuple(warnings),
        duplicate_keys=duplicates,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        mtime_ns=mtime_ns,
        ino=ino,
    )


def _analyse(
    line: Line, state: _State, path: str, key_path: str, seen: dict[str, int]
) -> Task | None:
    """Update `state` with `line` and return its task, if it is one."""
    text = line.text
    marker = _OWNED_MARKER.fullmatch(text)
    if marker:
        if marker.group("edge") == "begin":
            state.owned = marker.group("block")
        elif state.owned == marker.group("block"):
            state.owned = None
    if not state.comment:
        fence = _FENCE.match(text)
        if fence:
            quotes, width = _indent_width(fence.group("lead"))
            run, info = fence.group("fence"), fence.group("info")
        if state.fence is not None:
            if (
                fence
                and width - state.fence_base < 4
                and run[0] == state.fence[0]
                and len(run) >= len(state.fence)
                and not info.strip()
            ):
                state.fence = None
            return None
        if fence and not (run[0] == "`" and "`" in info):
            fence_base = state.base(quotes, width)
            if width - fence_base < 4:
                state.fence, state.fence_line, state.fence_base = run, line.number, fence_base
                return None
    toggles = _comment_toggles(text)
    if state.comment:
        state.comment = not toggles
        return None
    if toggles:
        state.comment, state.comment_line = True, line.number
    heading = _HEADING.match(text)
    if heading:
        state.heading = _heading_text(text, heading.end("level"))
    item = _LIST_ITEM.match(text)
    if item is None or (item.group("space") == "" and item.end() < len(text)):
        if text.strip() and not text.startswith((" ", "\t", ">")):
            state.stack.clear()  # a paragraph at the margin ends the lists above it
        return None
    quotes, width = _indent_width(item.group("prefix"))
    while state.stack and (state.stack[-1][0] != quotes or state.stack[-1][1] >= width):
        state.stack.pop()
    parent = state.stack[-1][2] if state.stack else None
    depth = len(state.stack)
    spaces = len(item.group("space").expandtabs(4))
    content = width + len(item.group("marker")) + (spaces if 1 <= spaces <= 4 else 1)
    state.stack.append((quotes, width, line.number, content))
    match = _TASK.fullmatch(text)
    if match is None:
        return None
    offset = match.start("body")
    before = text[offset - 1] if offset else ""
    trailer = _fields(match.group("body"), offset, before)
    digest = _digest(trailer.description)
    occurrence = seen.get(digest, 0)
    seen[digest] = occurrence + 1  # every task counts, whatever its key
    ids = [f.value for f in trailer.fields if f.kind == "id" and f.valid]
    return Task(
        line=line,
        path=path,
        prefix=match.group("prefix"),
        marker=match.group("marker"),
        status_char=match.group("status"),
        status_offset=match.start("status"),
        body_offset=offset,
        description=trailer.description,
        fields=tuple(trailer.fields),
        fields_end=trailer.fields_end,
        tags=_tags(match.group("body")),
        block_id=trailer.block_id,
        block_span=trailer.block_span,
        depth=depth,
        parent=parent,
        heading=state.heading,
        owned_block=state.owned,
        key=_key(key_path, ids, trailer.block_id, digest, occurrence),
    )


__all__ = [
    "ID",
    "MAX_FIELDS",
    "MAX_ID",
    "MAX_LINES",
    "MAX_LINE_BYTES",
    "PRIORITIES",
    "REMINDER",
    "SIGNIFIERS",
    "STATUSES",
    "Document",
    "Field",
    "Line",
    "Lines",
    "ParseWarning",
    "RecurringTaskError",
    "Reminder",
    "Task",
    "json_safe",
    "parse",
]
