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
  (`note_missing`) and bytes that only add to it.
- **An owned block** is the core's `<!-- horae:begin <block> -->` … `<!-- horae:end <block> -->`
  pair. In a note Horae does not own, `note.replace_owned_block` (and `restore`, for
  `horae undo`) may only change the lines of **one** such block that is already there:
  the bytes before and after it must be identical, the block must be exactly one well-formed
  marker pair before and after (`block_ambiguous` otherwise, the core's recogniser and
  rules: any case, a marker naming the block on a line that is not exactly the canonical
  marker counts), and the new block holds no other marker. Anything else is `not_owned`,
  as is creating or removing a note Horae does not own.

**Paths.** The vault is the plugin's single `paths.write` entry as approved in `horae.lock`
(`vault_unset`, `config_invalid`); the target must also pass `Paths.allowed(..., write=True)`
(`outside_vault`). A note path is relative and `/`-separated, ends in `.md`, with no empty,
`.`, `..` or hidden segment (so never `.obsidian/` or `.trash/`), no `\\`, `:` or control
character, at most 1024 characters and 255 a segment (`path_invalid`). Every component is
looked up in its directory's listing first: a name that exists only in another case or
Unicode normalisation form, or that has such a twin, is refused (`path_collision`), since
macOS, Windows and the phones a synced vault reaches would take them for one file. Then it
is opened relative to its parent's descriptor with `O_NOFOLLOW`, so a symbolic link
anywhere below the vault is refused (`symlink`); the note must be a regular file
(`not_a_file`) with a single hard link (`hard_link`), at most `MAX_NOTE_BYTES`
(`note_too_large`).

**Writes** follow the core's `LocalFileSink`: compare-and-swap on the SHA-256 of the bytes
read (`target_changed`, nothing created), then the missing parent directories (`mkdir`
relative to the parent descriptor, each new entry's directory `fsync`ed), then a temporary
file `.horae-<12 hex>.tmp` in the note's directory (`O_CREAT | O_EXCL | O_NOFOLLOW`, the
note's permission bits, or 0644 for a new note), written in full and `fsync`ed; just before
it is renamed over the note (`os.replace` relative to the directory descriptor) the note's
device, inode, size and modification time are checked against what was read
(`target_changed`), then the directory is `fsync`ed. Any failure removes the temporary file
and leaves the note as it was. A removal renames the note to a private `.horae-<hex>.old`,
checks what it renamed and unlinks it (or puts it back). Temporary files older than
`STALE_TMP_S` (left by a crash) are swept from every directory a write touches. As for the
core, this narrows but does not close the window in which an editor or a sync client
writing the same note at that moment loses its write.
"""

import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

from horae_sdk import Paths, SinkError
from horae_sdk.protocol import MAX_NOTE_BYTES

OWNED_SUFFIX = ".assistant.md"
MARKER = re.compile(r"horae:(begin|end)(?:\s+([a-z0-9-]+))?", re.IGNORECASE)
"""The core's block-marker recogniser (`horae.apply.notes.MARKER`)."""
NOTE_TYPES = ("note.create", "note.append_section", "note.replace_owned_block")
STALE_TMP_S = 3600.0
MAX_PATH = 1024
MAX_SEGMENT = 255
MAX_IDENTITY = 400
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_MODE = 0o644
_TMP = re.compile(r"^\.horae-[0-9a-f]{12}\.tmp$")
_BAD_CHAR = re.compile(r"[\\:\x00-\x1f\x7f]")
_CHUNK = 1024 * 1024
_SAME = ("st_dev", "st_ino", "st_size", "st_mtime_ns")


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


def _confined(before: bytes, after: bytes, blocks: Iterable[str], path: str) -> None:
    """Refuse `after` unless it differs from `before` only inside one of `blocks`."""
    if before == after:
        return
    old, new = _text(before, path), _text(after, path)
    for block in sorted(set(blocks)):
        was, now = block_span(old, block, path), block_span(new, block, path)
        if was is None or now is None:
            continue
        if old[: was[0]] != new[: now[0]] or old[was[1] :] != new[now[1] :]:
            continue
        inside = new[now[0] : now[1]]
        if len(MARKER.findall(inside)) != 2:
            raise SinkError("block_ambiguous", f"{path}: the new block {block!r} holds a marker")
        return
    raise _not_owned(path, "Horae does not own this note and the change is not inside a block")


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
        if not isinstance(block, str):
            raise SinkError("intent_invalid", "note.replace_owned_block needs its block")
        if not owned:
            if before is None:
                raise _not_owned(path, "Horae does not own this note and it does not exist")
            _confined(before, after, [block], path)
    else:
        raise SinkError("not_supported", f"the vault sink applies no {kind!s:.40} intent")


def check_restore(path: str, before: bytes | None, after: bytes | None) -> None:
    """Refuse an undo that would create, remove or change, outside one block, a note Horae
    does not own (the journal does not say which block: any block named in the note)."""
    if is_owned(path) or before == after:
        return
    if before is None or after is None:
        raise _not_owned(path, "Horae does not own this note: it neither creates nor removes it")
    names = {
        (match.group(2) or "").lower()
        for text in (_text(before, path), _text(after, path))
        for match in MARKER.finditer(text)
        if match.group(2)
    }
    _confined(before, after, names, path)


# --- paths --------------------------------------------------------------------------------------


def note_parts(path: object) -> list[str]:
    """The segments of a note path (`path_invalid` if it is not one)."""
    if not isinstance(path, str) or not 0 < len(path) <= MAX_PATH:
        raise SinkError("path_invalid", "not a note path")
    parts = path.split("/")
    if (
        not path.endswith(".md")
        or _BAD_CHAR.search(path)
        or any(not part or part.startswith(".") or len(part) > MAX_SEGMENT for part in parts)
    ):
        raise SinkError("path_invalid", "not a note path (relative, no hidden segment, .md)")
    return parts


def vault_of(paths: Paths) -> str:
    """The vault: the single approved `paths.write` entry."""
    if not paths.write:
        raise SinkError("vault_unset", "no paths.write entry approved for the obsidian plugin")
    if len(paths.write) != 1:
        raise SinkError("config_invalid", "the obsidian plugin needs exactly one paths.write")
    return paths.write[0]


def _target(paths: Paths, path: object) -> tuple[str, list[str]]:
    parts = note_parts(path)
    vault = vault_of(paths)
    if paths.allowed(os.path.join(vault, *parts), write=True) is None:
        raise SinkError("outside_vault", "the note is not inside the approved vault")
    return vault, parts


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _lookup(dir_fd: int, name: str, path: str) -> bool:
    """Whether `name` exists in `dir_fd` under exactly that name; `path_collision` when the
    listing holds it in another case or normalisation form, or holds a twin of it."""
    try:
        names = os.listdir(dir_fd)
    except OSError as error:
        raise SinkError("unreadable", f"{path}: a directory cannot be listed") from error
    key = _key(name)
    twins = [entry for entry in names if entry != name and _key(entry) == key]
    if twins:
        raise SinkError(
            "path_collision",
            f"{path}: the vault holds this name in another case or Unicode form",
        )
    return name in names


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
    vault, parts = _target(paths, path)
    shown = "/".join(parts)
    fds = [_open_vault(vault)]
    try:
        if _walk(parts, shown, fds):
            return None
        return _read_at(fds[-1], parts[-1], shown)[0]
    finally:
        _close(fds)


def parent_exists(paths: Paths, path: object) -> bool:
    vault, parts = _target(paths, path)
    fds = [_open_vault(vault)]
    try:
        return not _walk(parts, "/".join(parts), fds)
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
    vault, parts = _target(paths, path)
    shown = "/".join(parts)
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
        _replace(fds[-1], parts[-1], shown, after, seen)
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
    """Remove the temporary files a crash left in `dir_fd` (older than `STALE_TMP_S`)."""
    with contextlib.suppress(OSError):
        names = [name for name in os.listdir(dir_fd) if _TMP.fullmatch(name)]
        cutoff = time.time() - STALE_TMP_S
        for name in names:
            with contextlib.suppress(OSError):
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    os.unlink(name, dir_fd=dir_fd)


def _unchanged(dir_fd: int, name: str, seen: os.stat_result | None) -> bool:
    """Whether `name` is still the file `seen` (`None`: still missing)."""
    try:
        now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return seen is None
    return seen is not None and all(getattr(now, f) == getattr(seen, f) for f in _SAME)


def _replace(dir_fd: int, name: str, path: str, data: bytes, seen: os.stat_result | None) -> None:
    """Write `data` to a temporary file next to `name`, `fsync` it, and rename it over `name`
    if `name` is still the file `seen` (`None`: still missing); then `fsync` the directory."""
    mode = _NEW_MODE if seen is None else stat.S_IMODE(seen.st_mode)
    tmp = _hidden(".tmp")
    try:
        fd = os.open(tmp, _TMP_FLAGS, 0o600, dir_fd=dir_fd)
    except OSError as error:
        raise _write_error(path, error) from None
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not _unchanged(dir_fd, name, seen):
            raise _changed(path)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except (OSError, SinkError) as error:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        if isinstance(error, SinkError):
            raise
        raise _write_error(path, error) from None
    _sync(dir_fd)


def _remove(dir_fd: int, name: str, path: str, expected: str | None) -> None:
    """Remove `name` if it holds bytes of SHA-256 `expected`: rename it to a private name,
    check what was renamed, then unlink it (or put it back, never over a new note)."""
    hidden = _hidden(".old")
    try:
        os.rename(name, hidden, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except FileNotFoundError:
        raise _changed(path) from None
    except OSError as error:
        raise _write_error(path, error) from None
    try:
        if sha256(_read_at(dir_fd, hidden, path)[0]) != expected:
            raise _changed(path)
        os.unlink(hidden, dir_fd=dir_fd)
    except (OSError, SinkError) as error:
        try:
            os.link(hidden, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            os.unlink(hidden, dir_fd=dir_fd)
        except OSError:
            message = f"{path} changed during the undo; what was there is kept as {hidden}"
            raise SinkError("target_changed", message) from None
        if isinstance(error, SinkError):
            raise
        raise _write_error(path, error) from None
    _sync(dir_fd)


__all__ = [
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
