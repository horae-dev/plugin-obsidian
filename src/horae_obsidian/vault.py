# SPDX-License-Identifier: Apache-2.0
"""Read notes from a vault directory without leaving it (POSIX only).

The vault is a trusted path (the plugin's approved `paths` in `horae.lock`); every name
**inside** it is untrusted (anyone who syncs the vault writes them). So:

- a note path is relative, `/`-separated, with no empty, `.`, `..` or hidden (`.obsidian`,
  `.trash`) segment, and ends with `.md` (lower case: `.MD` is not a note);
- every component is opened relative to its parent's descriptor with `O_NOFOLLOW`: a
  **symbolic link is never followed**, anywhere below the vault (even one that points back
  inside it), so a link to `/etc` or to another vault cannot be read;
- only regular files are read, at most `max_bytes` (`MAX_NOTE_BYTES`, 4 MiB) each: a larger
  note is refused (`too_large`) before its content is read past the cap;
- `iter_scan` / `scan` walk the vault from its descriptor without following links
  (`os.fwalk`), skip hidden directories, go at most `max_depth` directories deep, stop after
  `max_notes` notes and read at most `max_total_bytes` and `max_total_tasks` in all (the
  notes past the budget are listed as `budget`, not read), and report every note or
  directory they skipped with the reason, so one bad file never stops a scan. A reported
  path escapes control and bidi characters (`safe_path`); `Document.path` stays raw for I/O.

The bytes are parsed by `horae_obsidian.tasks.parse` (BOM, CRLF and invalid UTF-8 kept).
"""

import errno
import os
import stat
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from horae_obsidian.tasks import Document, parse

MAX_NOTE_BYTES = 4 * 1024 * 1024
MAX_NOTES = 10_000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TOTAL_TASKS = 200_000
"""Bytes are not memory: a 4 MiB note of short tasks holds 100k of them (about 1 KB each
parsed), so a scan also stops reading once it holds this many tasks."""
MAX_DEPTH = 64
SUFFIX = ".md"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_VAULT = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 1024 * 1024


def safe_path(text: str) -> str:
    """`text` with control, format (bidi, zero-width), separator and surrogate characters
    escaped (`\\x1b`, `\\u202e`, a non-UTF-8 byte as `\\xNN`), and `\\` as `\\\\`."""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif 0xDC80 <= code <= 0xDCFF:  # an escaped byte (surrogateescape)
            out.append(f"\\x{code - 0xDC00:02x}")
        elif unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"):
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


class VaultError(Exception):
    """A note that cannot be read: `kind` is `invalid_path`, `not_found`, `symlink`,
    `not_a_file`, `not_a_directory`, `too_large`, `budget` or `unreadable`. The message
    escapes the path (`safe_path`)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Skipped:
    """A note or directory a scan did not read. `path` is escaped (`safe_path`); `kind` is a
    `VaultError` kind, `too_deep`, `internal` (an unexpected error, its type name in
    `detail`) or, for the marker `iter_scan` yields last when it stops early, `truncated`."""

    path: str
    kind: str
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class Scan:
    documents: tuple[Document, ...]
    skipped: tuple[Skipped, ...]
    truncated: bool
    """True when the scan stopped early: more than `max_notes` notes, or notes left unread
    past `max_total_bytes` or `max_total_tasks` (listed in `skipped` as `budget`)."""
    duplicate_keys: tuple[str, ...] = ()
    """Task keys held by more than one task of the vault (those tasks are not
    `key_unique`): two notes with the same 🆔, or the same block id in one note."""


def note_parts(path: str) -> list[str]:
    """The segments of a note path (`VaultError("invalid_path")` if it is not one)."""
    segments = path.split("/")
    if (
        not segments
        or "\0" in path
        or not path.endswith(SUFFIX)
        or any(not s or s.startswith(".") for s in segments)
    ):
        raise VaultError("invalid_path", f"not a note path: {safe_path(path)!r}")
    return segments


def _error(error: OSError, path: str) -> VaultError:
    shown = safe_path(path)
    if error.errno == errno.ELOOP:
        return VaultError("symlink", f"{shown}: a symbolic link is never followed")
    if error.errno == errno.ENOENT:
        return VaultError("not_found", f"{shown}: no such note")
    if error.errno in (errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
        return VaultError("not_a_file", f"{shown}: not a regular file")
    return VaultError("unreadable", f"{shown}: {os.strerror(error.errno or 0)}")


def _read_fd(
    fd: int, path: str, max_bytes: int, budget: int | None
) -> tuple[bytes, os.stat_result]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise VaultError("not_a_file", f"{safe_path(path)}: not a regular file")
    if info.st_size > max_bytes:
        raise VaultError("too_large", f"{safe_path(path)}: larger than {max_bytes} bytes")
    if budget is not None and info.st_size > budget:
        raise VaultError("budget", f"{safe_path(path)}: past the scan's byte budget")
    cap = max_bytes if budget is None else min(max_bytes, budget)
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(fd, min(_CHUNK, cap + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > cap:  # it grew while being read
            kind = "too_large" if size > max_bytes else "budget"
            raise VaultError(kind, f"{safe_path(path)}: it grew past {cap} bytes")
    return b"".join(chunks), info


def _read_at(
    dir_fd: int, name: str, path: str, max_bytes: int, budget: int | None = None
) -> Document:
    try:
        fd = os.open(name, _FILE, dir_fd=dir_fd)
    except OSError as error:
        raise _error(error, path) from None
    try:
        data, info = _read_fd(fd, path, max_bytes, budget)
    except OSError as error:
        raise _error(error, path) from None
    finally:
        os.close(fd)
    return parse(data, path, mtime_ns=info.st_mtime_ns, ino=info.st_ino)


def _open_vault(vault: str | Path) -> int:
    try:
        return os.open(vault, _VAULT)
    except OSError as error:
        shown = safe_path(os.fsdecode(vault))
        if error.errno == errno.ENOENT:
            raise VaultError("not_found", f"{shown}: no such vault") from None
        if error.errno == errno.ENOTDIR:
            raise VaultError("not_a_directory", f"{shown}: not a directory") from None
        raise VaultError("unreadable", f"{shown}: {os.strerror(error.errno or 0)}") from None


def read_note(vault: str | Path, path: str, *, max_bytes: int = MAX_NOTE_BYTES) -> Document:
    """The note at `path` (relative to `vault`), parsed. `VaultError` when it cannot be read
    safely (see the module docstring)."""
    parts = note_parts(path)
    fds = [_open_vault(vault)]
    try:
        for part in parts[:-1]:
            fds.append(os.open(part, _DIRECTORY, dir_fd=fds[-1]))
        return _read_at(fds[-1], parts[-1], path, max_bytes)
    except OSError as error:
        raise _error(error, path) from None
    finally:
        for fd in fds:
            os.close(fd)


def iter_scan(
    vault: str | Path,
    *,
    max_bytes: int = MAX_NOTE_BYTES,
    max_notes: int = MAX_NOTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_total_tasks: int = MAX_TOTAL_TASKS,
    max_depth: int = MAX_DEPTH,
) -> Iterator[Document | Skipped]:
    """Every note of `vault` (each directory's notes by name, then its subdirectories),
    parsed, or `Skipped`, one at a time: at most one note is held. Keys are checked for
    duplicates within each note only (`scan` checks the whole vault). When it stops at
    `max_notes`, the last item is `Skipped("", "truncated")`. `VaultError` if `vault` is
    not a readable directory."""
    root = _open_vault(vault)
    errors: list[Skipped] = []

    def failed(error: OSError) -> None:
        name = os.fsdecode(error.filename) if error.filename is not None else ""
        name = "" if name == "." else name.removeprefix("./")
        errors.append(Skipped(safe_path(name), _error(error, name).kind))

    notes = 0
    budget, tasks_left = max_total_bytes, max_total_tasks
    exhausted = False
    walk = os.fwalk(".", dir_fd=root, follow_symlinks=False, onerror=failed)
    try:
        for top, dirs, files, dir_fd in walk:
            yield from errors
            errors.clear()
            relative = top.removeprefix(".").removeprefix("/")
            prefix = relative + "/" if relative else ""
            depth = relative.count("/") + 1 if relative else 0
            kept = []
            for name in sorted(dirs):
                if name.startswith("."):
                    continue
                try:
                    mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
                except OSError as error:
                    yield Skipped(safe_path(prefix + name), _error(error, prefix + name).kind)
                    continue
                if stat.S_ISLNK(mode):
                    yield Skipped(safe_path(prefix + name), "symlink")
                elif depth + 1 > max_depth:
                    yield Skipped(safe_path(prefix + name), "too_deep")
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(files):
                if name.startswith(".") or not name.endswith(SUFFIX):
                    continue
                if notes >= max_notes:
                    yield Skipped("", "truncated")
                    return
                notes += 1
                path = prefix + name
                if exhausted:
                    yield Skipped(safe_path(path), "budget")
                    continue
                try:
                    document = _read_at(dir_fd, name, path, max_bytes, budget)
                except VaultError as error:
                    if error.kind == "budget":
                        exhausted = True
                    yield Skipped(safe_path(path), error.kind)
                    continue
                except Exception as error:  # one bad note never stops a scan
                    yield Skipped(safe_path(path), "internal", type(error).__name__)
                    continue
                if len(document.tasks) > tasks_left:
                    exhausted = True
                    yield Skipped(safe_path(path), "budget")
                    continue
                budget -= document.size
                tasks_left -= len(document.tasks)
                yield document
        yield from errors
    finally:
        walk.close()  # type: ignore[attr-defined]  # a generator: closes its descriptors
        os.close(root)


def scan(
    vault: str | Path,
    *,
    max_bytes: int = MAX_NOTE_BYTES,
    max_notes: int = MAX_NOTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_total_tasks: int = MAX_TOTAL_TASKS,
    max_depth: int = MAX_DEPTH,
) -> Scan:
    """Every note of `vault` (sorted by path), parsed, without following any link; task keys
    held more than once across the vault are flagged (`Scan.duplicate_keys`)."""
    documents: list[Document] = []
    skipped: list[Skipped] = []
    truncated = False
    for item in iter_scan(
        vault,
        max_bytes=max_bytes,
        max_notes=max_notes,
        max_total_bytes=max_total_bytes,
        max_total_tasks=max_total_tasks,
        max_depth=max_depth,
    ):
        if isinstance(item, Document):
            documents.append(item)
        elif item.kind == "truncated":
            truncated = True
        else:
            truncated = truncated or item.kind == "budget"
            skipped.append(item)
    documents.sort(key=lambda d: d.path)
    skipped.sort(key=lambda s: s.path)
    counts = Counter(task.key for document in documents for task in document.tasks)
    duplicates = tuple(sorted(key for key, count in counts.items() if count > 1))
    if duplicates:
        documents = [_flag(document, counts) for document in documents]
    return Scan(tuple(documents), tuple(skipped), truncated, duplicates)


def _flag(document: Document, counts: Counter[str]) -> Document:
    if all(counts[task.key] == 1 for task in document.tasks):
        return document
    tasks = tuple(
        replace(task, key_unique=False) if counts[task.key] > 1 else task for task in document.tasks
    )
    return replace(document, tasks=tasks)


__all__ = [
    "MAX_DEPTH",
    "MAX_NOTES",
    "MAX_NOTE_BYTES",
    "MAX_TOTAL_BYTES",
    "MAX_TOTAL_TASKS",
    "Scan",
    "Skipped",
    "VaultError",
    "iter_scan",
    "note_parts",
    "read_note",
    "safe_path",
    "scan",
]
