# SPDX-License-Identifier: Apache-2.0
"""Read Obsidian Tasks lines from a Markdown note, keeping every byte.

This is the reading counterpart of `horae_sdk.tasks` (which escapes titles and writes
`task_line`s): it knows the same signifiers (`SIGNIFIERS`, each with an optional U+FE0F).

**Nothing is rewritten.** `parse(data)` splits a note's bytes into `Line`s that keep their
exact text, line ending and byte span; `Document.render()` gives back the identical bytes,
and `Document.replace_line(task, text)` changes only that line's span (the status edit of
`task.update_status`). Bytes that are not UTF-8 are kept as lone surrogates
(`surrogateescape`) and written back as they were; a UTF-8 BOM is kept.

**What is a task** (the Tasks plugin's own rule, `[ \\t>]*` then a list marker `-` `*` `+`
or `1.` / `1)`, a space, `[c]` with one status character, then a space or the end of the
line), except in frontmatter, fenced code blocks (backticks or tildes, also inside
blockquotes and lists) and `%%` comment blocks. Blockquotes and callouts (`> - [ ]`) and
nested lists are tasks too.

**Fields** are read the Tasks way: from the **end** of the line backwards, as long as the
end is a field (`📅 2026-10-22`, `⏰ 19:00`, `🔁 every day`, a priority…), a trailing tag or,
first, a block id (`^abc`). The text before them is the description; a signifier in the
middle of the text is text. A field whose value does not parse (`📅 2026-02-30`) is kept as
**unknown** (`Field.valid` false, in `Task.unknown`), never interpreted. The ⏰ reminder is
Horae's `⏰ HH:MM` (the time on the due date, `horae_sdk.tasks.task_line`); the Reminder
plugin's `⏰ YYYY-MM-DD HH:MM` and `⏰ YYYY-MM-DD` are read too.

**Ids.** `Task.key` is a stable fingerprint (`horae_sdk.intents.fingerprint`, the
`task.update_status` alphabet) that survives ticking, a new ✅ date, edited dates and lines
added above: the Tasks `🆔` id when there is one (vault-wide), else the block id (per note),
else a digest of the note path and the description, with its occurrence among identical
descriptions of the note. The hidden ids read are `🆔 id` and `^block-id`.

Vault text is **untrusted** (threat model T02): the parser only reads it; a description is
never fed back to a Tasks line without `horae_sdk.tasks.escape_title`.
"""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

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

STATUSES: dict[str, Status] = {
    " ": "todo",
    "x": "done",
    "X": "done",
    "/": "in_progress",
    "-": "cancelled",
}
"""Status characters with a meaning; any other (`[>]`, `[?]`…) is `custom`, kept as is."""

MAX_FIELDS = 64
"""At most this many trailing fields and tags are read per line (the rest is text)."""

_LINE_END = re.compile(r"\r\n|\r|\n")
_TASK = re.compile(
    r"(?P<prefix>[ \t>]*)(?P<marker>[-*+]|[0-9]{1,9}[.)])[ \t]+"
    r"\[(?P<status>[^\]\r\n])\](?P<body>(?:[ \t].*)?)",
    re.DOTALL,
)
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_TIME = r"[0-9]{2}:[0-9]{2}"
_ID_CHARS = r"[A-Za-z0-9_-]+"


def _signifier(chars: str) -> str:
    return f"(?P<sig>[{chars}])(?P<vs>{VS16}?)"


_FIELD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_signifier("".join(DATE_SIGNIFIERS)) + rf" *(?P<value>{_DATE})$"), "date"),
    (
        re.compile(
            _signifier(REMINDER) + rf" *(?P<value>{_DATE}(?: +{_TIME})?|{_TIME})$",
        ),
        "reminder",
    ),
    (re.compile(_signifier(RECURRENCE) + r" *(?P<value>[a-zA-Z0-9, !]*[a-zA-Z0-9!])$"), "text"),
    (re.compile(_signifier("".join(PRIORITIES)) + r"(?P<value>)$"), "priority"),
    (re.compile(_signifier(ID) + rf" *(?P<value>{_ID_CHARS})$"), "text"),
    (
        re.compile(
            _signifier(DEPENDS_ON) + rf" *(?P<value>{_ID_CHARS}(?: *, *{_ID_CHARS})*)$",
        ),
        "list",
    ),
    (re.compile(_signifier(ON_COMPLETION) + r" *(?P<value>[a-zA-Z]+)$"), "text"),
]
_BLOCK_ID = re.compile(r"(?<=[ \t])\^(?P<value>[A-Za-z0-9-]+)$")
_TAG_CHARS = r"[^\s!@#$%^&*(),.?\":{}|<>\[\]`'\\;=+~]"
_TAG = re.compile(rf"(?<![^\s])#(?P<tag>{_TAG_CHARS}+)")
_TRAILING_TAG = re.compile(rf"(?<![^\s])#(?P<tag>{_TAG_CHARS}+)$")
_MASKED = re.compile(
    r"(?P<code>`+)(?:.*?)(?P=code)"  # inline code
    r"|%%.*?%%"  # inline comment
    r"|!?\[\[.*?\]\]"  # wikilink, embed
    r"|!?\[[^\]]*\]\([^)]*\)"  # Markdown link, image
    r"|<[^>\s]*>"  # autolink
    r"|[A-Za-z][A-Za-z0-9+.-]*://\S*",  # bare URL
)
_FENCE = re.compile(r"[ \t>]*(?P<fence>`{3,}|~{3,})(?P<info>.*)")
_FRONTMATTER_OPEN = re.compile(r"---[ \t]*")
_FRONTMATTER_CLOSE = re.compile(r"(?:---|\.\.\.)[ \t]*")
_COMMENT = "%%"


def _real_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _real_time(text: str) -> bool:
    hours, minutes = text.split(":")
    return int(hours) < 24 and int(minutes) < 60


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class Field:
    """A trailing field of a task line, at `start:end` (character offsets in the line)."""

    kind: Kind
    signifier: str
    """As written: the emoji and its U+FE0F if any."""
    value: str
    """The raw value text (`""` for a priority)."""
    text: str
    """The field's exact text in the line."""
    start: int
    end: int
    valid: bool
    """False when the value does not parse (a date that does not exist, `⏰ 25:00`)."""


@dataclass(frozen=True)
class Reminder:
    """`⏰ HH:MM` (on the due date), `⏰ YYYY-MM-DD HH:MM` or `⏰ YYYY-MM-DD`."""

    date: date | None
    time: str | None


@dataclass(frozen=True)
class Task:
    """A task line. `line.text` is the exact original text: render it, never rebuild it."""

    line: Line
    path: str
    prefix: str
    """Indentation and blockquote markers before the list marker (`"  "`, `"> "`)."""
    marker: str
    """`-`, `*`, `+`, or `1.` / `1)`."""
    status_char: str
    status_offset: int
    """Character offset of the status character in `line.text`."""
    body: str
    """Everything after `]`."""
    description: str
    """The body without its trailing fields and block id, stripped (trailing tags kept)."""
    fields: tuple[Field, ...]
    """Trailing fields, left to right, invalid ones included."""
    tags: tuple[str, ...]
    """`#tags` of the body (not in code, links, URLs or comments), in order, with `#`."""
    block_id: str | None
    depth: int
    """Enclosing list items (0: top level)."""
    parent: int | None
    """Line number of the enclosing list item, if any."""
    key: str
    """Stable fingerprint (see the module docstring)."""

    @property
    def status(self) -> Status:
        return STATUSES.get(self.status_char, "custom")

    def _last(self, kind: Kind) -> Field | None:
        # Tasks reads from the end: the leftmost valid field of a kind wins.
        found = [f for f in self.fields if f.kind == kind and f.valid]
        return found[0] if found else None

    def _date(self, kind: Kind) -> date | None:
        found = self._last(kind)
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
        found = self._last("reminder")
        if found is None:
            return None
        parts = found.value.split()
        if ":" in parts[-1]:
            day = date.fromisoformat(parts[0]) if len(parts) == 2 else None
            return Reminder(day, parts[-1])
        return Reminder(date.fromisoformat(parts[0]), None)

    @property
    def recurrence(self) -> str | None:
        found = self._last("recurrence")
        return found.value if found else None

    @property
    def priority(self) -> Priority | None:
        found = self._last("priority")
        return PRIORITIES[found.signifier[0]] if found else None

    @property
    def id(self) -> str | None:
        found = self._last("id")
        return found.value if found else None

    @property
    def depends_on(self) -> tuple[str, ...]:
        found = self._last("depends_on")
        return tuple(p.strip() for p in found.value.split(",")) if found else ()

    @property
    def on_completion(self) -> str | None:
        found = self._last("on_completion")
        return found.value if found else None

    @property
    def unknown(self) -> tuple[str, ...]:
        """The text of the fields whose value does not parse, kept, never interpreted."""
        return tuple(f.text for f in self.fields if not f.valid)

    def render(self) -> str:
        """The line's text, unchanged (a parsed task is never rebuilt from its fields)."""
        return self.line.text

    def with_status(self, char: str) -> str:
        """The line with only its status character replaced (`x`, ` `, `-`, `/`)."""
        if len(char) != 1 or char in "]\r\n":
            raise ValueError("a status is one character, not `]` or a line break")
        text, at = self.line.text, self.status_offset
        return text[:at] + char + text[at + 1 :]


@dataclass(frozen=True)
class Document:
    """A parsed note: its bytes, its lines and its tasks."""

    path: str
    data: bytes
    bom: bool
    lines: tuple[Line, ...]
    tasks: tuple[Task, ...] = field(repr=False)
    invalid_utf8: bool

    def render(self) -> bytes:
        """The note's bytes, rebuilt from its lines: always `data`."""
        text = "".join(line.text + line.eol for line in self.lines)
        return (BOM if self.bom else b"") + text.encode("utf-8", ENCODING_ERRORS)

    def replace_line(self, task: Task, text: str) -> bytes:
        """The note's bytes with only `task`'s line text replaced (its ending kept)."""
        if _LINE_END.search(text):
            raise ValueError("a line cannot hold a line break")
        line = task.line
        if self.lines[line.number - 1] != line:
            raise ValueError("the task is not a line of this document")
        new = text.encode("utf-8", ENCODING_ERRORS)
        return self.data[: line.start] + new + self.data[line.end :]


def split_lines(text: str, offset: int = 0) -> Iterator[Line]:
    """`text`'s lines with their endings and byte spans (from `offset`)."""
    position, number = 0, 1
    for match in _LINE_END.finditer(text):
        yield _line(number, text[position : match.start()], match.group(), offset)
        offset += len(text[position : match.end()].encode("utf-8", ENCODING_ERRORS))
        position, number = match.end(), number + 1
    if position < len(text) or number == 1:
        yield _line(number, text[position:], "", offset)


def _line(number: int, text: str, eol: str, offset: int) -> Line:
    size = len(text.encode("utf-8", ENCODING_ERRORS))
    return Line(number, text, eol, offset, offset + size)


def _masked(body: str) -> str:
    """`body` with code, comments, links and URLs blanked out (same length)."""
    return _MASKED.sub(lambda m: "\0" * len(m.group()), body)


def _tags(body: str) -> tuple[str, ...]:
    found = (m.group("tag") for m in _TAG.finditer(_masked(body)))
    return tuple(f"#{tag}" for tag in found if not tag.isdigit())


def _valid(kind: str, value: str) -> bool:
    if kind == "date":
        return _real_date(value) is not None
    if kind == "reminder":
        parts = value.split()
        return all(_real_time(p) if ":" in p else _real_date(p) is not None for p in parts)
    return True


def _fields(body: str, offset: int) -> tuple[str, list[Field], str | None]:
    """The description, the trailing fields (left to right) and the block id of `body`
    (which starts at character `offset` of the line)."""
    rest = body.rstrip()
    block_id = None
    match = _BLOCK_ID.search(rest)
    if match:
        block_id, rest = match.group("value"), rest[: match.start()].rstrip()
    fields: list[Field] = []
    trailing_tags: list[str] = []
    for _ in range(MAX_FIELDS):
        tag = _TRAILING_TAG.search(rest)
        if tag and not tag.group("tag").isdigit():
            trailing_tags.insert(0, tag.group())
            rest = rest[: tag.start()].rstrip()
            continue
        found = next(
            ((m, rule) for pattern, rule in _FIELD_RULES if (m := pattern.search(rest))), None
        )
        if found is None:
            break
        match, rule = found
        value = match.group("value")
        fields.insert(
            0,
            Field(
                kind=SIGNIFIERS[match.group("sig")],
                signifier=match.group("sig") + match.group("vs"),
                value=value,
                text=match.group(),
                start=offset + match.start(),
                end=offset + match.end(),
                valid=_valid(rule, value),
            ),
        )
        rest = rest[: match.start()].rstrip()
    description = " ".join([rest.strip(), *trailing_tags]).strip()
    return description, fields, block_id


def _indent_width(prefix: str) -> tuple[int, int]:
    """(blockquote depth, indentation width after the last `>`, a tab as 4)."""
    quotes = prefix.count(">")
    after = prefix.rsplit(">", 1)[-1]
    if quotes:
        after = after[1:] if after.startswith(" ") else after
    return quotes, sum(4 if c == "\t" else 1 for c in after)


_LIST_ITEM = re.compile(r"(?P<prefix>[ \t>]*)(?:[-*+]|[0-9]{1,9}[.)])(?:[ \t]|$)")


def _content_lines(lines: tuple[Line, ...]) -> Iterator[Line]:
    """The lines that can hold a task: not frontmatter, fenced code or a `%%` block."""
    index = 0
    if lines and _FRONTMATTER_OPEN.fullmatch(lines[0].text):
        for close in range(1, len(lines)):
            if _FRONTMATTER_CLOSE.fullmatch(lines[close].text):
                index = close + 1
                break
    fence: str | None = None
    comment = False
    for line in lines[index:]:
        if not comment:
            match = _FENCE.match(line.text)
            if (
                fence is None
                and match
                and not (match.group("fence")[0] == "`" and "`" in match.group("info"))
            ):
                fence = match.group("fence")
                continue
            if fence is not None:
                if (
                    match
                    and match.group("fence")[0] == fence[0]
                    and len(match.group("fence")) >= len(fence)
                    and not match.group("info").strip()
                ):
                    fence = None
                continue
        opens = line.text.count(_COMMENT) % 2 == 1
        if comment:
            comment = not opens
            continue
        yield line
        comment = opens


def _digest(description: str) -> str:
    normal = " ".join(description.split())
    return hashlib.sha256(normal.encode("utf-8", ENCODING_ERRORS)).hexdigest()[:16]


def parse(data: bytes, path: str = "") -> Document:
    """`data` (a note's bytes) parsed: never raises, never loses a byte (`render()`)."""
    bom = data.startswith(BOM)
    raw = data[len(BOM) :] if bom else data
    text = raw.decode("utf-8", ENCODING_ERRORS)
    invalid = any("\udc80" <= c <= "\udcff" for c in text)
    lines = tuple(split_lines(text, len(BOM) if bom else 0))
    tasks: list[Task] = []
    seen: dict[str, int] = {}
    stack: list[tuple[int, int, int]] = []  # (quote depth, indent width, line number)
    for line in _content_lines(lines):
        item = _LIST_ITEM.match(line.text)
        if item is None:
            if line.text.strip() and not line.text.startswith((" ", "\t", ">")):
                stack.clear()  # a paragraph at the margin ends the lists above it
            continue
        quotes, width = _indent_width(item.group("prefix"))
        while stack and (stack[-1][0] != quotes or stack[-1][1] >= width):
            stack.pop()
        parent = stack[-1][2] if stack else None
        depth = len(stack)
        stack.append((quotes, width, line.number))
        match = _TASK.fullmatch(line.text)
        if match is None:
            continue
        body = match.group("body")
        offset = match.start("body")
        description, fields, block_id = _fields(body, offset)
        ids = [f.value for f in fields if f.kind == "id" and f.valid]
        if ids:  # the Tasks id, vault-wide
            key = fingerprint(NAME, "id", ids[0])
        elif block_id is not None:  # a block id, unique in its note
            key = fingerprint(NAME, "block", path or "-", block_id)
        else:  # the description, counted among identical ones of this note
            digest = _digest(description)
            occurrence = seen.get(digest, 0)
            seen[digest] = occurrence + 1
            key = fingerprint(NAME, "task", path or "-", digest, str(occurrence))
        tasks.append(
            Task(
                line=line,
                path=path,
                prefix=match.group("prefix"),
                marker=match.group("marker"),
                status_char=match.group("status"),
                status_offset=match.start("status"),
                body=body,
                description=description,
                fields=tuple(fields),
                tags=_tags(body),
                block_id=block_id,
                depth=depth,
                parent=parent,
                key=key,
            )
        )
    return Document(path, data, bom, lines, tuple(tasks), invalid)


__all__ = [
    "ID",
    "PRIORITIES",
    "REMINDER",
    "SIGNIFIERS",
    "STATUSES",
    "Document",
    "Field",
    "Line",
    "Reminder",
    "Task",
    "parse",
    "split_lines",
]
