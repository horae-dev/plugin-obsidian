# SPDX-License-Identifier: Apache-2.0
"""Read notes from a vault directory without leaving it.

The vault is a trusted path (the plugin's approved `paths` in `horae.lock`); every name
**inside** it is untrusted (anyone who syncs the vault writes them). So:

- a note path is relative, `/`-separated, with no empty, `.`, `..` or hidden (`.obsidian`,
  `.trash`) segment, and ends with `.md`;
- every component is opened relative to its parent's descriptor with `O_NOFOLLOW`: a
  **symbolic link is never followed**, anywhere below the vault (even one that points back
  inside it), so a link to `/etc` or to another vault cannot be read;
- only regular files are read, at most `max_bytes` (`MAX_NOTE_BYTES`, 4 MiB) each: a larger
  note is refused (`too_large`) before its content is read past the cap;
- `scan` walks the vault without following links (`os.fwalk`), skips hidden directories,
  reads at most `max_notes` notes and reports every note it skipped with the reason, so one
  bad file never stops a scan.

The bytes are parsed by `horae_obsidian.tasks.parse` (BOM, CRLF and invalid UTF-8 kept).
"""

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from horae_obsidian.tasks import Document, parse

MAX_NOTE_BYTES = 4 * 1024 * 1024
MAX_NOTES = 10_000
SUFFIX = ".md"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 1024 * 1024


class VaultError(Exception):
    """A note that cannot be read: `kind` is `invalid_path`, `not_found`, `symlink`,
    `not_a_file`, `too_large` or `unreadable`."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Skipped:
    path: str
    kind: str


@dataclass(frozen=True)
class Scan:
    documents: tuple[Document, ...]
    skipped: tuple[Skipped, ...]
    truncated: bool
    """True when the vault holds more than `max_notes` notes (the rest were not read)."""


def note_parts(path: str) -> list[str]:
    """The segments of a note path (`VaultError("invalid_path")` if it is not one)."""
    segments = path.split("/")
    if (
        not segments
        or "\0" in path
        or not path.endswith(SUFFIX)
        or any(not s or s.startswith(".") for s in segments)
    ):
        raise VaultError("invalid_path", f"not a note path: {path!r}")
    return segments


def _error(error: OSError, path: str) -> VaultError:
    if error.errno == errno.ELOOP:
        return VaultError("symlink", f"{path}: a symbolic link is never followed")
    if error.errno == errno.ENOENT:
        return VaultError("not_found", f"{path}: no such note")
    if error.errno in (errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
        return VaultError("not_a_file", f"{path}: not a regular file")
    return VaultError("unreadable", f"{path}: {os.strerror(error.errno or 0)}")


def _read_fd(fd: int, path: str, max_bytes: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise VaultError("not_a_file", f"{path}: not a regular file")
    if info.st_size > max_bytes:
        raise VaultError("too_large", f"{path}: larger than {max_bytes} bytes")
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(fd, min(_CHUNK, max_bytes + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > max_bytes:  # it grew while being read
            raise VaultError("too_large", f"{path}: larger than {max_bytes} bytes")
    return b"".join(chunks)


def _read_at(dir_fd: int, name: str, path: str, max_bytes: int) -> bytes:
    try:
        fd = os.open(name, _FILE, dir_fd=dir_fd)
    except OSError as error:
        raise _error(error, path) from None
    try:
        return _read_fd(fd, path, max_bytes)
    except OSError as error:
        raise _error(error, path) from None
    finally:
        os.close(fd)


def read_note(vault: str | Path, path: str, *, max_bytes: int = MAX_NOTE_BYTES) -> Document:
    """The note at `path` (relative to `vault`), parsed. `VaultError` when it cannot be read
    safely (see the module docstring)."""
    parts = note_parts(path)
    fds = [os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        for part in parts[:-1]:
            try:
                fds.append(os.open(part, _DIRECTORY, dir_fd=fds[-1]))
            except OSError as error:
                raise _error(error, path) from None
        return parse(_read_at(fds[-1], parts[-1], path, max_bytes), path)
    finally:
        for fd in fds:
            os.close(fd)


def scan(vault: str | Path, *, max_bytes: int = MAX_NOTE_BYTES, max_notes: int = MAX_NOTES) -> Scan:
    """Every note of `vault` (sorted by path), parsed, without following any link."""
    documents: list[Document] = []
    skipped: list[Skipped] = []
    truncated = False
    for top, dirs, files, dir_fd in os.fwalk(vault, follow_symlinks=False):
        relative = os.path.relpath(top, vault)
        prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
        kept = []
        for name in sorted(dirs):
            if name.startswith("."):
                continue
            if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                skipped.append(Skipped(prefix + name, "symlink"))
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            if name.startswith(".") or not name.endswith(SUFFIX):
                continue
            if len(documents) >= max_notes:
                truncated = True
                break
            path = prefix + name
            try:
                documents.append(parse(_read_at(dir_fd, name, path, max_bytes), path))
            except VaultError as error:
                skipped.append(Skipped(path, error.kind))
    documents.sort(key=lambda d: d.path)
    skipped.sort(key=lambda s: s.path)
    return Scan(tuple(documents), tuple(skipped), truncated)


__all__ = [
    "MAX_NOTES",
    "MAX_NOTE_BYTES",
    "Scan",
    "Skipped",
    "VaultError",
    "note_parts",
    "read_note",
    "scan",
]
