# SPDX-License-Identifier: Apache-2.0
"""Write notes into the vault: only Horae's own files and blocks, atomically (POSIX only).

**Who decides what.** The core computes every note's bytes (`horae.apply.notes.note_after`:
ownership, block markers, line endings) and sends them to `obsidian.apply` with the SHA-256
the note must still have (`expected_before_sha256`, `None`: it must not exist). This module
never computes a note; it writes the core's bytes, and refuses them when they would touch
anything Horae does not own, as a second, independent check (threat model T30):

- **An owned note** is one whose file name ends in `.assistant.md` (the core's rule, decided
  on the path, which is resolved without following any link). `note.create` needs an owned
  note that does not exist (`note_exists`), `note.append_section` an owned note that exists
  (`note_missing`) and bytes that only add to it. `note.replace_owned_block` in an owned note
  must leave the block's marker pair there exactly once, holding no other marker, and keep
  the rest of the note (a new block only follows the note's bytes).
- **An owned block** is the core's `<!-- horae:begin <block> -->` … `<!-- horae:end <block> -->`
  pair (`<block>`: the core's `BlockId`, `intent_invalid` otherwise). In a note Horae does not
  own, `note.replace_owned_block` may only change the lines of its block, which must already
  be there, one well-formed marker pair before and after (`block_ambiguous` otherwise, the
  core's recogniser and rules: any case, a marker naming the block on a line that is not
  exactly the canonical marker counts, so does one in a code fence), holding no other
  marker. `restore` (`horae undo`) does not know which blocks the run replaced: the bytes
  outside the well-formed pairs (each one pair holding no other marker, in both texts) must
  be identical, so a run that replaced several blocks, or one block several times, is undone;
  a malformed marker elsewhere is ignored when the blocks explain the change
  (`block_ambiguous` when they do not). Anything else is `not_owned`, as is creating or
  removing a note Horae does not own; so is `horae undo --force` over a note that is to be
  created or removed. A forced restore of a note edited outside its blocks (`merge_forced`)
  puts the blocks back, removes a block the original lacks and writes a deleted one again
  where it was (`_anchor`); the user's bytes outside the blocks stay.

**Paths.** The vault is the plugin's single `paths.write` entry as approved in `horae.lock`,
an absolute path (`vault_unset`, `config_invalid`); the target must also pass
`Paths.allowed(..., write=True)` (`outside_vault`). A note path is relative and
`/`-separated, ends in `.md`, with no empty, `.`, `..` or hidden segment (so never
`.obsidian/` or `.trash/`), no `\\`, `:`, control character or lone surrogate, at most 1024
characters and 255 UTF-8 bytes a segment (`path_invalid`). Every component is looked up in
its directory's listing first: a name that exists only in another case or Unicode
normalisation form, or that has such a twin, is refused (`path_collision`, naming the name
on disk), since macOS, Windows and the phones a synced vault reaches would take them for one
file. Then it is opened relative to its parent's descriptor with `O_NOFOLLOW`, so a symbolic
link anywhere below the vault is refused (`symlink`); the note must be a regular file
(`not_a_file`) with a single hard link (`hard_link`), at most `MAX_NOTE_BYTES`
(`note_too_large`). Messages escape the paths (`horae_obsidian.vault.safe_path`).

**Writes** follow the core's `LocalFileSink`: compare-and-swap on the SHA-256 of the bytes
read (`target_changed`, nothing created), then the missing parent directories (`mkdir`
relative to the parent descriptor, each new entry's directory `fsync`ed), then a temporary
file `.horae-<12 hex>.tmp` in the note's directory (`O_CREAT | O_EXCL | O_NOFOLLOW`, the
note's permission bits, or 0644 for a new note), written in full and `fsync`ed. A new note
is then `os.link`ed in place, which never replaces a note created meanwhile (`note_exists`
or `target_changed`; a file system without hard links falls back to the rename). An
existing one is renamed over (`os.replace` relative to the directory descriptor) once its
device, inode, size and modification time are checked against what was read, and its bytes
re-read and hashed (a same-size edit within one clock tick), all under a process-wide lock
(`target_changed`); then the directory is `fsync`ed. Any failure, or an interruption, removes
the temporary file and leaves the note as it was. A removal renames the note to a private
`.horae-<hex>.old`, checks what it renamed and unlinks it (or links it back). Temporary files
older than `STALE_TMP_S` (left by a crash) are swept from every directory a write touches, and
so are such `.old` files whose bytes are also linked elsewhere (never the only copy). As for
the core, this narrows but does not close the window in which an editor or a sync client
writing the same note at that moment loses its write.
"""

import bisect
import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from horae_sdk import Paths, SinkError
from horae_sdk.protocol import MAX_NOTE_BYTES

from horae_obsidian.vault import safe_path

OWNED_SUFFIX = ".assistant.md"
MARKER = re.compile(r"horae:(begin|end)(?:\s+([a-z0-9-]+))?", re.IGNORECASE)
"""The core's block-marker recogniser (`horae.apply.notes.MARKER`)."""
BLOCK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
"""A block id (the core's `BlockId`)."""
NOTE_TYPES = ("note.create", "note.append_section", "note.replace_owned_block")
STALE_TMP_S = 3600.0
MAX_PATH = 1024
MAX_SEGMENT = 255
MAX_IDENTITY = 400
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_MODE = 0o644
_STALE = re.compile(r"^\.horae-[0-9a-f]{12}\.(tmp|old)$")
_BAD_CHAR = re.compile(r"[\\:\x00-\x1f\x7f]")
_CHUNK = 1024 * 1024
_SAME = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
_NO_LINK = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK}
"""`os.link` errors that mean "this file system makes no hard link": rename instead."""
_COMMIT = threading.Lock()
"""Held from the last check of a note to its rename: two writes of this process never both
pass the check of the same note."""


# --- ownership --------------------------------------------------------------------------------


def is_owned(path: str) -> bool:
    """Whether Horae owns the note at `path` (its file name ends in `.assistant.md`)."""
    return path.rsplit("/", 1)[-1].endswith(OWNED_SUFFIX)


def begin_marker(block: str) -> str:
    return f"<!-- horae:begin {block} -->"


def end_marker(block: str) -> str:
    return f"<!-- horae:end {block} -->"


def _lines(text: str) -> Iterable[str]:
    """`text` split after each `\\n` only (a `\\r` stays in its line), as the core does."""
    start = 0
    while (stop := text.find("\n", start)) != -1:
        yield text[start : stop + 1]
        start = stop + 1
    if start < len(text):
        yield text[start:]


def block_span(text: str, block: str, path: str) -> tuple[int, int] | None:
    """The span of block `block`'s lines in `text` (both markers included); `None` when no
    line names it. `block_ambiguous` unless it is exactly one canonical marker pair."""
    begin, end = begin_marker(block), end_marker(block)
    begins: list[tuple[int, int]] = []
    ends: list[tuple[int, int]] = []
    offset = 0
    for line in _lines(text):
        stop = offset + len(line)
        names = [m for m in MARKER.finditer(line) if (m.group(2) or "").lower() == block]
        if names:
            bare = line.rstrip("\n").rstrip("\r").strip(" \t")
            if len(names) != 1 or bare not in (begin, end):
                raise SinkError("block_ambiguous", f"{path}: a marker of {block!r} is malformed")
            (begins if bare == begin else ends).append((offset, stop))
        offset = stop
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0][0] < begins[0][1]:
        raise SinkError("block_ambiguous", f"{path}: block {block!r} is not one marker pair")
    return begins[0][0], ends[0][1]


def _text(data: bytes, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SinkError("note_not_text", f"{path} is not UTF-8 text") from None


def _not_owned(path: str, why: str) -> SinkError:
    return SinkError("not_owned", f"{path}: {why}")


@dataclass
class _Marks:
    """One text's markers, found in a single pass: per block name its canonical begin and
    end lines and whether any line names it otherwise; the start of every marker."""

    begins: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    ends: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    bad: set[str] = field(default_factory=set)
    starts: list[int] = field(default_factory=list)

    def names(self) -> set[str]:
        return set(self.begins) | set(self.ends) | self.bad

    def span(self, block: str) -> tuple[int, int] | None:
        """`block`'s span if it is one well-formed pair holding no other marker."""
        begins, ends = self.begins.get(block, []), self.ends.get(block, [])
        if block in self.bad or len(begins) != 1 or len(ends) != 1:
            return None
        (start, first), (last, stop) = begins[0], ends[0]
        if last < first:
            return None
        inside = bisect.bisect_left(self.starts, stop) - bisect.bisect_left(self.starts, start)
        return (start, stop) if inside == 2 else None


def _marks(text: str) -> _Marks:
    found = _Marks()
    offset = 0
    for line in _lines(text):
        stop = offset + len(line)
        matches = list(MARKER.finditer(line))
        if matches:
            found.starts.extend(offset + m.start() for m in matches)
            bare = line.rstrip("\n").rstrip("\r").strip(" \t")
            named = Counter((m.group(2) or "").lower() for m in matches if m.group(2))
            for name, count in named.items():
                if count != 1 or bare not in (begin_marker(name), end_marker(name)):
                    found.bad.add(name)
                else:
                    edges = found.begins if bare == begin_marker(name) else found.ends
                    edges.setdefault(name, []).append((offset, stop))
        offset = stop
    return found


def _outside(
    text: str, spans: list[tuple[str, tuple[int, int]]], lone: list[tuple[int, int]] | None = None
) -> list[str]:
    """The pieces of `text` around `spans` (in order), each followed by its block's name.
    The `lone` spans (a block only this text holds, `_confined(..., lone=True)`) are left out
    as if they were not there."""
    items: list[tuple[int, int, str | None]] = [(a, b, n) for n, (a, b) in spans]
    items += [(start, stop, None) for start, stop in lone or []]
    pieces: list[str] = []
    held = ""
    last = 0
    for start, stop, name in sorted(items, key=lambda item: item[0]):
        held += text[last:start]
        if name is not None:
            pieces += [held, name]
            held = ""
        last = stop
    pieces.append(held + text[last:])
    return pieces


def _lone(marks: _Marks, other: _Marks) -> list[tuple[int, int]]:
    """The spans of the blocks only `marks` holds: well-formed pairs no line of `other` names."""
    spans = [(name, marks.span(name)) for name in marks.names() - other.names()]
    return [span for _, span in spans if span is not None]


def _confined(
    before: bytes, after: bytes, allowed: set[str] | None, path: str, *, lone: bool = False
) -> None:
    """Refuse `after` unless the bytes outside the well-formed block pairs are identical in
    both texts and every block that changed is in `allowed` (`None`: any) and, in both texts,
    one well-formed marker pair holding no other marker. A malformed marker elsewhere is
    ignored when the blocks explain the change, `block_ambiguous` when they do not. With
    `lone` (only `horae undo --force`), a block that is one well-formed pair in one text and
    not named at all in the other (added or removed whole) is not "outside" either."""
    if before == after:
        return
    old, new = _text(before, path), _text(after, path)
    was, now = _marks(old), _marks(new)
    names = (was.names() | now.names()) if allowed is None else allowed
    pairs: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    for name in names:
        then, later = was.span(name), now.span(name)
        if then is not None and later is not None:
            pairs.append((name, then, later))
    old_spans = sorted(((name, then) for name, then, _ in pairs), key=lambda item: item[1])
    new_spans = sorted(((name, later) for name, _, later in pairs), key=lambda item: item[1])
    gone, added = (_lone(was, now), _lone(now, was)) if lone else ([], [])
    if _outside(old, old_spans, gone) == _outside(new, new_spans, added):
        return
    unclear = {
        name
        for name in names
        for marks in (was, now)
        if name in marks.names() and marks.span(name) is None
    }
    if unclear:
        shown = ", ".join(repr(name) for name in sorted(unclear)[:3])
        raise SinkError("block_ambiguous", f"{path}: block {shown} is not one marker pair")
    raise _not_owned(path, "Horae does not own this note and the change is not inside a block")


def _check_owned_block(before: bytes | None, after: bytes, block: str, path: str) -> None:
    """A replaced block of an owned note: the pair is there exactly once and holds no other
    marker; the rest of the note is kept (a new block only follows the note's bytes)."""
    new = _text(after, path)
    span = block_span(new, block, path)
    if span is None or _marks(new).span(block) is None:
        raise SinkError("block_ambiguous", f"{path}: block {block!r} is not one marker pair")
    if before is None:
        return
    old = _text(before, path)
    was = block_span(old, block, path)
    if was is None:
        if not new.startswith(old) or span[1] != len(new):
            raise _not_owned(path, "a new block may only follow the note's bytes")
    elif old[: was[0]] != new[: span[0]] or old[was[1] :] != new[span[1] :]:
        raise _not_owned(path, "the bytes outside the block must be kept")


def check_apply(intent: dict[str, Any], before: bytes | None, after: bytes) -> None:
    """Refuse the core's `after` for `intent` when it writes what Horae does not own."""
    kind, path = intent.get("type"), str(intent.get("path"))
    owned = is_owned(path)
    if kind == "note.create":
        if not owned:
            raise _not_owned(path, f"only a *{OWNED_SUFFIX} note is created")
        if before is not None:
            raise SinkError("note_exists", f"{path} already exists")
    elif kind == "note.append_section":
        if not owned:
            raise _not_owned(path, f"sections are appended to *{OWNED_SUFFIX} notes only")
        if before is None:
            raise SinkError("note_missing", f"{path} does not exist")
        if not after.startswith(before):
            raise _not_owned(path, "an appended section must leave the note before it intact")
    elif kind == "note.replace_owned_block":
        block = intent.get("block")
        if not isinstance(block, str) or not BLOCK_ID.fullmatch(block):
            raise SinkError("intent_invalid", "note.replace_owned_block needs a valid block id")
        if owned:
            _check_owned_block(before, after, block, path)
            return
        if before is None:
            raise _not_owned(path, "Horae does not own this note and it does not exist")
        old, new = _text(before, path), _text(after, path)
        block_span(old, block, path)  # the core refuses these too: never written
        block_span(new, block, path)
        _confined(before, after, {block}, path)
    else:
        raise SinkError("not_supported", f"the vault sink applies no {kind!s:.40} intent")


def check_restore(
    path: str, before: bytes | None, after: bytes | None, *, forced: bool = False
) -> None:
    """Refuse an undo that would create, remove or change, outside its blocks, a note Horae
    does not own (the journal does not say which blocks a run replaced: any of them may
    change, each one well-formed pair before and after). `forced` (`merge_forced`'s result
    only) also accepts a block removed or added whole."""
    if is_owned(path) or before == after:
        return
    if before is None or after is None:
        raise _not_owned(path, "Horae does not own this note: it neither creates nor removes it")
    _confined(before, after, None, path, lone=forced)


def _anchor(old: str, start: int, text: str, path: str) -> int:
    """Where in `text` (the note as it is now) the block that began at `start` in `old` goes
    back: after the one line that preceded it (a non-blank line `text` holds exactly once, with
    its line ending), else at the very start or the very end of the note when the block was
    there. `not_owned` when the place is not certain: nothing is guessed."""
    if start == 0:
        if text.startswith("\ufeff"):
            raise _not_owned(path, "the deleted block began the note, which now has a BOM")
        return 0
    previous = old[: start - 1].rsplit("\n", 1)[-1] + "\n"
    if previous.strip():
        offset, found = 0, []
        for line in _lines(text):
            offset += len(line)
            if line == previous:
                found.append(offset)
        if len(found) == 1:
            return found[0]
    raise _not_owned(path, "the place of a block the user deleted cannot be told")


def merge_forced(path: str, current: bytes, original: bytes) -> bytes:
    """`horae undo --force` of a note Horae does not own, edited outside its blocks since: the
    note as it is now, with each block put back as `original` has it. Only blocks that are one
    well-formed pair in both texts are replaced; the bytes outside them stay the user's. A
    block `original` lacks (Horae created it) is removed whole, markers and content. A block
    the user deleted is written again at the place `original` records (`_anchor`: after the
    one unmistakable line that preceded it, or at the very start or end of the note); in
    doubt it is `not_owned`. Anything else is `not_owned` (or `block_ambiguous`)."""
    old, new = _text(original, path), _text(current, path)
    was, now = _marks(old), _marks(new)
    pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    removed: list[tuple[int, int]] = []
    restored: list[tuple[int, int]] = []
    for name in was.names() | now.names():
        then, later = was.span(name), now.span(name)
        if then is not None and later is not None:
            pairs.append((then, later))
        elif then is None and later is not None and name not in was.names():
            removed.append(later)
        elif then is not None and later is None and name not in now.names():
            restored.append(then)
        elif any(name in m.names() and m.span(name) is None for m in (was, now)):
            raise SinkError("block_ambiguous", f"{path}: a block is not one marker pair")
        else:
            raise _not_owned(path, "Horae does not own this note and the blocks are not the same")
    edits = [(later, old[then[0] : then[1]]) for then, later in pairs]
    edits += [(span, "") for span in removed]
    text = new
    for (start, stop), content in sorted(edits, key=lambda edit: edit[0][0], reverse=True):
        text = text[:start] + content + text[stop:]
    for then in sorted(restored):  # in the order of the original: a block can anchor the next
        at = _anchor(old, then[0], text, path)
        text = text[:at] + old[then[0] : then[1]] + text[at:]
    merged = text.encode("utf-8")
    _confined(current, merged, None, path, lone=True)  # the blocks alone changed
    return merged


def forced_restore(paths: Paths, path: str, content: bytes | None, expected: str | None) -> None:
    """`restore` under `horae undo --force`. A restore the unforced rules accept is the same
    write as before. One they refuse as `not_owned` (the note was edited outside Horae's
    blocks) puts back the blocks only (`merge_forced`); the write stays a compare-and-swap on
    the bytes the core read, and a note to create or remove stays refused."""
    current = read(paths, path)
    if sha256(current) != expected:
        raise _changed(str(path))
    wanted = content
    merged = False
    try:
        check_restore(str(path), current, content)
    except SinkError as error:
        if error.kind != "not_owned" or current is None or content is None:
            raise
        wanted, merged = merge_forced(str(path), current, content), True

    def check(before: bytes | None) -> None:
        check_restore(str(path), before, wanted, forced=merged)

    put(paths, path, wanted, expected, check)


# --- paths --------------------------------------------------------------------------------------


def note_parts(path: object) -> list[str]:
    """The segments of a note path (`path_invalid` if it is not one)."""
    if not isinstance(path, str) or not 0 < len(path) <= MAX_PATH:
        raise SinkError("path_invalid", "not a note path")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate: no file name
        raise SinkError("path_invalid", "not a note path (not UTF-8)") from None
    parts = path.split("/")
    if (
        not path.endswith(".md")
        or _BAD_CHAR.search(path)
        or any(
            not part or part.startswith(".") or len(part.encode("utf-8")) > MAX_SEGMENT
            for part in parts
        )
    ):
        raise SinkError("path_invalid", "not a note path (relative, no hidden segment, .md)")
    return parts


def vault_of(paths: Paths) -> str:
    """The vault: the single approved `paths.write` entry, an absolute path."""
    if not paths.write:
        raise SinkError("vault_unset", "no paths.write entry approved for the obsidian plugin")
    if len(paths.write) != 1:
        raise SinkError("config_invalid", "the obsidian plugin needs exactly one paths.write")
    vault = paths.write[0]
    if not os.path.isabs(vault):
        raise SinkError(
            "config_invalid",
            f"the obsidian plugin's paths.write must be an absolute path, not {safe_path(vault)}",
        )
    return vault


def _target(paths: Paths, path: object) -> tuple[str, list[str], str]:
    """The vault, the note's segments and the note path escaped for messages."""
    parts = note_parts(path)
    vault = vault_of(paths)
    if paths.allowed(os.path.join(vault, *parts), write=True) is None:
        raise SinkError("outside_vault", "the note is not inside the approved vault")
    return vault, parts, safe_path("/".join(parts))


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _lookup(dir_fd: int, name: str, path: str) -> bool:
    """Whether `name` exists in `dir_fd` under exactly that name; `path_collision` when the
    listing holds it in another case or normalisation form, or holds a twin of it.

    The listing is needed to see twins; each entry costs one comparison: an ASCII name is
    its own NFC form and its case fold is `lower()`, so only other names are normalised."""
    try:
        names = os.listdir(dir_fd)
    except OSError as error:
        raise SinkError("unreadable", f"{path}: a directory cannot be listed") from error
    key = _key(name)
    size = len(key)
    found = False
    for entry in names:
        if entry == name:
            found = True
        elif (
            (len(entry) == size and entry.lower() == key) if entry.isascii() else _key(entry) == key
        ):
            raise SinkError(
                "path_collision",
                f'{path}: the vault holds "{safe_path(entry)}" for this name (another case or'
                " Unicode form); rename it on disk or use that exact name",
            )
    return found


def _is_link(name: str, dir_fd: int) -> bool:
    """Whether `name` is a symbolic link (`O_DIRECTORY | O_NOFOLLOW` on a link: macOS)."""
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open(name: str, flags: int, dir_fd: int, path: str) -> int:
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP or (error.errno == errno.ENOTDIR and _is_link(name, dir_fd)):
            raise SinkError("symlink", f"{path} goes through a symbolic link") from None
        if error.errno in (errno.ENOTDIR, errno.EISDIR, errno.ENXIO, errno.ENAMETOOLONG):
            raise SinkError("not_a_file", f"{path}: not a regular note") from None
        if error.errno == errno.ENOENT:
            raise SinkError("target_changed", f"{path} vanished while it was opened") from None
        raise SinkError("unreadable", f"{path} cannot be opened ({error.strerror})") from None


def _open_vault(vault: str) -> int:
    try:
        return os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as error:
        raise SinkError("unreadable", f"the vault cannot be opened ({error.strerror})") from None


def _walk(parts: list[str], path: str, fds: list[int]) -> list[str]:
    """Open the note's existing parent directories (appended to `fds`); the missing ones."""
    for position, part in enumerate(parts[:-1]):
        if not _lookup(fds[-1], part, path):
            return parts[position:-1]
        fds.append(_open(part, _DIR_FLAGS, fds[-1], path))
    return []


def _read_at(dir_fd: int, name: str, path: str) -> tuple[bytes | None, os.stat_result | None]:
    """The bytes and `fstat` of note `name` in `dir_fd` (`None`s: it does not exist)."""
    if not _lookup(dir_fd, name, path):
        return None, None
    return _read_file(dir_fd, name, path)


def _read_file(dir_fd: int, name: str, path: str) -> tuple[bytes, os.stat_result]:
    """The bytes and `fstat` of the regular, single-link file `name` in `dir_fd`."""
    fd = _open(name, _FILE_FLAGS, dir_fd, path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SinkError("not_a_file", f"{path} is not a regular file")
        if info.st_nlink > 1:
            raise SinkError("hard_link", f"{path} has more than one hard link")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, _CHUNK):
            size += len(chunk)
            if size > MAX_NOTE_BYTES:
                raise _too_large(path)
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _too_large(path: str) -> SinkError:
    return SinkError("note_too_large", f"{path} is larger than {MAX_NOTE_BYTES} bytes")


def _close(fds: list[int]) -> None:
    for fd in reversed(fds):
        os.close(fd)


# --- operations ---------------------------------------------------------------------------------


def sha256(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def identity(paths: Paths) -> str:
    """`vault:<st_dev>:<st_ino>:<real path>` of the vault (the path hashed when long)."""
    real = os.path.realpath(vault_of(paths))
    try:
        info = os.stat(real)
    except OSError as error:
        raise SinkError("unreadable", f"the vault cannot be opened ({error.strerror})") from None
    found = f"vault:{info.st_dev}:{info.st_ino}:{real}"
    if len(found) > MAX_IDENTITY:
        digest = hashlib.sha256(os.fsencode(real)).hexdigest()
        found = f"vault:{info.st_dev}:{info.st_ino}:sha256:{digest}"
    return found


def read(paths: Paths, path: object) -> bytes | None:
    """The note's bytes (`None`: it does not exist). Side-effect free."""
    vault, parts, shown = _target(paths, path)
    fds = [_open_vault(vault)]
    try:
        if _walk(parts, shown, fds):
            return None
        return _read_at(fds[-1], parts[-1], shown)[0]
    finally:
        _close(fds)


def parent_exists(paths: Paths, path: object) -> bool:
    vault, parts, shown = _target(paths, path)
    fds = [_open_vault(vault)]
    try:
        return not _walk(parts, shown, fds)
    finally:
        _close(fds)


def put(
    paths: Paths,
    path: object,
    after: bytes | None,
    expected: str | None,
    check: Callable[[bytes | None], None],
    *,
    creating: bool = False,
) -> None:
    """Compare-and-swap: if note `path` has SHA-256 `expected` (`None`: missing) and
    `check(before)` passes, make it hold `after` (`None`: remove it). `creating`: an
    existing note is `note_exists` rather than `target_changed`."""
    vault, parts, shown = _target(paths, path)
    if after is not None and len(after) > MAX_NOTE_BYTES:
        raise _too_large(shown)
    fds = [_open_vault(vault)]
    try:
        missing = _walk(parts, shown, fds)
        before, seen = (None, None) if missing else _read_at(fds[-1], parts[-1], shown)
        if sha256(before) != expected:
            if creating and before is not None:
                raise SinkError("note_exists", f"{shown} already exists")
            raise _changed(shown)
        check(before)
        if after is None:
            if before is not None:
                _sweep(fds[-1])
                _remove(fds[-1], parts[-1], shown, sha256(before))
            return
        _make_dirs(missing, shown, fds)
        _sweep(fds[-1])
        _replace(fds[-1], parts[-1], shown, after, (seen, expected), creating=creating)
    finally:
        _close(fds)


def _changed(path: str) -> SinkError:
    return SinkError("target_changed", f"{path} changed since it was planned")


def _write_error(path: str, error: OSError) -> SinkError:
    return SinkError("unwritable", f"{path} cannot be written ({error.strerror})")


def _hidden(suffix: str) -> str:
    return f".horae-{secrets.token_hex(6)}{suffix}"


def _sync(dir_fd: int) -> None:
    with contextlib.suppress(OSError):  # some file systems cannot fsync a directory
        os.fsync(dir_fd)


def _make_dirs(missing: list[str], path: str, fds: list[int]) -> None:
    for part in missing:
        created = True
        try:
            os.mkdir(part, 0o755, dir_fd=fds[-1])
        except FileExistsError:
            created = False  # created meanwhile: looked up and opened below
        except OSError as error:
            raise _write_error(path, error) from None
        _lookup(fds[-1], part, path)
        directory = _open(part, _DIR_FLAGS, fds[-1], path)
        if created:
            _sync(fds[-1])
        fds.append(directory)


def _sweep(dir_fd: int) -> None:
    """Remove what a crash left in `dir_fd` for more than `STALE_TMP_S`: temporary files,
    and `.old` names of a removal whose file is also linked elsewhere (a put-back whose last
    unlink failed). An `.old` file with a single link may be the only copy of a note (a
    put-back that failed): it is kept."""
    with contextlib.suppress(OSError):
        names = [name for name in os.listdir(dir_fd) if _STALE.fullmatch(name)]
        cutoff = time.time() - STALE_TMP_S
        for name in names:
            with contextlib.suppress(OSError):
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if name.endswith(".old") and (
                    info.st_nlink < 2 or max(info.st_mtime, info.st_ctime) >= cutoff
                ):
                    continue
                if info.st_mtime < cutoff:
                    os.unlink(name, dir_fd=dir_fd)


def _unchanged(dir_fd: int, name: str, seen: os.stat_result | None, digest: str | None) -> bool:
    """Whether `name` is still the file `seen` with SHA-256 `digest` (`None`: still missing):
    the same device, inode, size and modification time, and the same bytes (re-read: a
    same-size edit within the clock's resolution keeps the rest)."""
    try:
        now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return seen is None
    if seen is None or not all(getattr(now, f) == getattr(seen, f) for f in _SAME):
        return False
    try:
        return sha256(_read_file(dir_fd, name, name)[0]) == digest
    except SinkError:
        return False


def _link_new(dir_fd: int, tmp: str, name: str, path: str, *, creating: bool) -> bool:
    """Give the temporary file the missing note's name, never over a note created meanwhile
    (`os.link` fails if `name` exists); `False` if the file system makes no hard link."""
    try:
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        if creating:
            raise SinkError("note_exists", f"{path} was created meanwhile") from None
        raise _changed(path) from None
    except OSError as error:
        if error.errno in _NO_LINK:
            return False
        raise
    return True


def _replace(
    dir_fd: int,
    name: str,
    path: str,
    data: bytes,
    was: tuple[os.stat_result | None, str | None],
    *,
    creating: bool = False,
) -> None:
    """Write `data` to a temporary file next to `name` and `fsync` it; then, if `name` is
    still the file `seen` with SHA-256 `digest` (`was`; `None`: still missing), link it as
    the missing note or rename it over `name`; then `fsync` the directory. The temporary
    file never outlives the call, whatever stops it."""
    seen, digest = was
    mode = _NEW_MODE if seen is None else stat.S_IMODE(seen.st_mode)
    tmp = _hidden(".tmp")
    try:
        fd = os.open(tmp, _TMP_FLAGS, 0o600, dir_fd=dir_fd)
    except OSError as error:
        raise _write_error(path, error) from None
    renamed = False
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        with _COMMIT:
            if seen is None and _link_new(dir_fd, tmp, name, path, creating=creating):
                pass  # linked: the temporary name is removed below
            elif not _unchanged(dir_fd, name, seen, digest):
                raise _changed(path)
            else:
                os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                renamed = True
    except OSError as error:
        raise _write_error(path, error) from None
    finally:
        if not renamed:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
    _sync(dir_fd)


def _remove(dir_fd: int, name: str, path: str, expected: str | None) -> None:
    """Remove `name` if it holds bytes of SHA-256 `expected`: rename it to a private name,
    check what was renamed, then unlink it (or put it back, never over a new note)."""
    hidden = _hidden(".old")
    with _COMMIT:
        try:
            os.rename(name, hidden, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileNotFoundError:
            raise _changed(path) from None
        except OSError as error:
            raise _write_error(path, error) from None
        try:
            if sha256(_read_file(dir_fd, hidden, path)[0]) != expected:
                raise _changed(path)
            os.unlink(hidden, dir_fd=dir_fd)
        except (OSError, SinkError) as error:
            _put_back(dir_fd, hidden, name, path)
            if isinstance(error, SinkError):
                raise
            raise _write_error(path, error) from None
    _sync(dir_fd)


def _put_back(dir_fd: int, hidden: str, name: str, path: str) -> None:
    """Undo the rename of `name` to `hidden`, never over a file created at `name` meanwhile
    (the core's `_put_back`). Once linked back, failing to unlink `hidden` loses nothing:
    the sweep removes it later."""
    try:
        os.link(hidden, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        message = f"{path} changed during the undo; what was there is kept as {hidden}"
        raise SinkError("target_changed", message) from None
    with contextlib.suppress(OSError):
        os.unlink(hidden, dir_fd=dir_fd)


__all__ = [
    "BLOCK_ID",
    "MARKER",
    "NOTE_TYPES",
    "OWNED_SUFFIX",
    "STALE_TMP_S",
    "begin_marker",
    "block_span",
    "check_apply",
    "check_restore",
    "end_marker",
    "identity",
    "is_owned",
    "note_parts",
    "parent_exists",
    "put",
    "read",
    "vault_of",
]
