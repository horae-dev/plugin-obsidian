# SPDX-License-Identifier: Apache-2.0
"""The task index: Horae's block id → the fingerprint of the task it was written for.

Every task line the plugin creates (`task.create`) ends with the hidden block id
`^h-<12 hex>`, the first 12 hex digits of the SHA-256 of the intent's fingerprint
(`block_of`, the SDK's `horae_sdk.tasks.task_block_id`). A digest cannot be read back, so the
plugin keeps the index `block id → fingerprint`: `obsidian.open_tasks` reports a task with
its `source_fingerprint` (the `github:pr:42` of the task that created it), and
`task.update_status` finds a task by that fingerprint anywhere in the vault.

- **Where**: `.horae/obsidian-tasks.json` in the vault, the plugin's single approved
  `paths.write` directory (`horae.lock`). The vault walk skips hidden directories, so the
  index is never read as a note. The `.horae` directory is created with mode 0700 and must
  belong to the plugin's user and not be writable by group or others (`index_unavailable`),
  and be on a **local** file system (`flock` and `fsync` are not reliable on network
  shares).
- **Provenance**: each entry also records the note path and the SHA-256 of the exact line
  the Tasks writer wrote (`Link.path`, `Link.line_sha256`; updated when the sink changes that
  line). A block id is public (anyone who sees the fingerprint can compute it), so it never
  proves on its own that Horae wrote a line: only a line at that path with that digest is
  Horae's Tasks output (`tasksink.horae_output`).
- **Checked, not trusted**: an entry is used only when its block id is the hash of its
  fingerprint, so a hand-edited or synced-over index can drop links, never misdirect one.
  Losing the index loses only `source_fingerprint`: the block id still finds the task from
  its fingerprint (`task.update_status` hashes it).
- **Concurrent runs**: an exclusive `flock` on `.horae/obsidian-tasks.lock` is held from the
  read to the write (`locked`), at most `LOCK_TIMEOUT_S` (`index_busy`). Reading
  (`read`) takes no lock and writes nothing (a source is side-effect free).
- **Atomic**: written to a temporary file in the same directory, `fsync`ed, renamed over the
  old one, the directory `fsync`ed; temporary files a crash left are swept after an hour.
- **Fail closed** on a file that cannot be read (permission denied, a symbolic link) or in
  another `format` (`index_unavailable`); corrupt or oversized content starts over.
- **Bounded**: at most `MAX_ENTRIES` entries (the most recent kept) and `MAX_INDEX_BYTES`.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from horae_sdk import SinkError

from horae_obsidian.tasks import HORAE_BLOCK

DIRECTORY = ".horae"
INDEX_FILE = "obsidian-tasks.json"
LOCK_FILE = "obsidian-tasks.lock"
TEMPORARY_PREFIX = ".obsidian-tasks-"
TEMPORARY_SUFFIX = ".tmp"
FORMAT = 1
MAX_ENTRIES = 20_000
MAX_INDEX_BYTES = 4 * 1024 * 1024
STALE_TEMPORARY_S = 3600.0
LOCK_TIMEOUT_S = 8.0
LOCK_POLL_S = 0.05
FINGERPRINT = re.compile(r"[A-Za-z0-9._:-]{1,128}")
"""A `task.create` fingerprint (the contract's pattern)."""
_SHA = re.compile(r"[0-9a-f]{64}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00")


def block_of(fingerprint: str) -> str:
    """Horae's block id for `fingerprint`: `h-` and 12 hex digits of its SHA-256 (the SDK's
    `task_block_id`, repeated here so the plugin works with horae-sdk 0.2 too).
    `ValueError` when it is not a fingerprint."""
    if not FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("not a task fingerprint")
    return "h-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Link:
    """One entry: the fingerprint, when it was recorded, and where the line was written."""

    fingerprint: str
    at: datetime
    path: str | None = None
    """The note the Tasks writer wrote the line to."""
    line_sha256: str | None = None
    """The SHA-256 of the exact line written there (its ending excluded)."""

    def wrote(self, path: str, line_sha256: str) -> bool:
        """Whether this is the line the Tasks writer wrote (same note, same bytes)."""
        return self.path == path and self.line_sha256 == line_sha256


@dataclass
class Entries:
    """The index as read: block id → `Link`."""

    links: dict[str, Link] = field(default_factory=dict)
    reset: bool = False
    """The file held corrupt or oversized content and was started over."""

    def fingerprints(self) -> dict[str, str]:
        return {block: link.fingerprint for block, link in self.links.items()}


def _parse(raw: bytes | None) -> Entries:
    if raw is None:
        return Entries()
    try:
        document = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_INDEX_BYTES else None
    except (UnicodeDecodeError, ValueError):
        document = None
    if not isinstance(document, dict):
        return Entries(reset=True)
    version = document.get("format")
    if isinstance(version, int) and not isinstance(version, bool) and version != FORMAT:
        raise SinkError(
            "index_unavailable",
            f"{DIRECTORY}/{INDEX_FILE} is in a format this plugin does not read (written by a"
            " newer version?): upgrade the plugin, or remove the file",
        )
    links = document.get("tasks")
    if isinstance(version, bool) or version != FORMAT or not isinstance(links, dict):
        return Entries(reset=True)
    entries = Entries()
    for block, value in links.items():
        if not (isinstance(block, str) and HORAE_BLOCK.fullmatch(block)):
            continue
        if not isinstance(value, dict):
            continue
        fingerprint, stamp = value.get("fingerprint"), value.get("at")
        if not (isinstance(fingerprint, str) and isinstance(stamp, str)):
            continue
        if not (FINGERPRINT.fullmatch(fingerprint) and _STAMP.fullmatch(stamp)):
            continue
        if block_of(fingerprint) != block:  # checked, never trusted
            continue
        path, digest = value.get("path"), value.get("line_sha256")
        written = (
            isinstance(path, str)
            and 0 < len(path) <= 1024
            and isinstance(digest, str)
            and _SHA.fullmatch(digest) is not None
        )
        entries.links[block] = Link(
            fingerprint,
            datetime.fromisoformat(stamp),
            path if written and isinstance(path, str) else None,
            digest if written and isinstance(digest, str) else None,
        )
    return entries


class TaskIndex:
    """The index of the vault at `vault` (an absolute, trusted path)."""

    def __init__(
        self, vault: str, *, max_entries: int = MAX_ENTRIES, lock_timeout_s: float = LOCK_TIMEOUT_S
    ) -> None:
        self.directory = os.path.join(vault, DIRECTORY)
        self.path = os.path.join(self.directory, INDEX_FILE)
        self.lock_path = os.path.join(self.directory, LOCK_FILE)
        self.max_entries = max_entries
        self.lock_timeout_s = lock_timeout_s

    # --- reading (no lock, no write) ----------------------------------------------------------

    def read(self) -> Entries:
        """The index as it is now (empty when there is none). Side-effect free."""
        try:
            info = os.lstat(self.directory)
        except FileNotFoundError:
            return Entries()
        except OSError:
            raise self._unavailable() from None
        if not stat.S_ISDIR(info.st_mode):
            raise self._unavailable()
        return _parse(self._read())

    def _unavailable(self) -> SinkError:
        return SinkError(
            "index_unavailable",
            f"the task index {DIRECTORY}/{INDEX_FILE} in the vault cannot be read (permission"
            " denied, a symbolic link or not a regular file): give it to the plugin's user"
            " (mode 0600), or remove it",
        )

    def _read(self) -> bytes | None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise self._unavailable() from None
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise self._unavailable()
            return file.read(MAX_INDEX_BYTES + 1)

    # --- writing (under the lock) ----------------------------------------------------------

    @contextlib.contextmanager
    def locked(self) -> Iterator[Entries]:
        """Holds the exclusive lock for the block and yields the index as read under it."""
        self._prepare()
        try:
            fd = os.open(
                self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
        except OSError:
            raise SinkError(
                "index_unavailable",
                f"cannot open the task index lock {DIRECTORY}/{LOCK_FILE}: the vault must be"
                " writable by the obsidian plugin",
            ) from None
        try:
            deadline = time.monotonic() + self.lock_timeout_s
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise SinkError(
                        "index_busy",
                        f"another run held the task index for over {self.lock_timeout_s:g} s",
                    )
                time.sleep(LOCK_POLL_S)
            self._sweep()
            yield _parse(self._read())
        finally:
            os.close(fd)  # releases the lock

    def _prepare(self) -> None:
        """Creates `.horae` (0700) when missing; refuses one that is not the user's own."""
        with contextlib.suppress(FileExistsError):
            os.mkdir(self.directory, 0o700)
        try:
            info = os.lstat(self.directory)
        except OSError:
            info = None
        if (
            info is None
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise SinkError(
                "index_unavailable",
                f"the vault's {DIRECTORY} directory must be a directory of the user running the"
                " obsidian plugin, not writable by group or others (chown it; chmod 0700)",
            )

    def _sweep(self) -> None:
        cutoff = time.time() - STALE_TEMPORARY_S
        with contextlib.suppress(OSError):
            for name in os.listdir(self.directory):
                if name.startswith(TEMPORARY_PREFIX) and name.endswith(TEMPORARY_SUFFIX):
                    path = os.path.join(self.directory, name)
                    with contextlib.suppress(OSError):
                        if os.lstat(path).st_mtime < cutoff:
                            os.unlink(path)

    def add(
        self,
        entries: Entries,
        fingerprint: str,
        now: datetime,
        *,
        path: str | None = None,
        line_sha256: str | None = None,
    ) -> str:
        """Records `fingerprint` (and the note and line digest the Tasks writer wrote) in
        `entries` (call it under `locked`, then `save`) and returns its block id.
        `block_collision` when the block id already links another fingerprint (two
        fingerprints with the same 48-bit hash: never seen in practice)."""
        block = block_of(fingerprint)
        known = entries.links.get(block)
        if known is not None and known.fingerprint != fingerprint:
            raise SinkError(
                "block_collision", "another task fingerprint has the same Horae block id"
            )
        at = now.astimezone(UTC).replace(microsecond=0)
        entries.links[block] = Link(fingerprint, at, path, line_sha256)
        return block

    def save(self, entries: Entries) -> None:
        """Writes `entries` (under `locked`), the `max_entries` most recent kept."""
        ordered = sorted(entries.links.items(), key=lambda item: item[1].at)
        kept = ordered[-self.max_entries :] if self.max_entries else []
        entries.links = dict(kept)
        tasks: dict[str, dict[str, str]] = {}
        for block, link in entries.links.items():
            tasks[block] = {"fingerprint": link.fingerprint, "at": link.at.isoformat()}
            if link.path is not None and link.line_sha256 is not None:
                tasks[block].update(path=link.path, line_sha256=link.line_sha256)
        data = json.dumps({"format": FORMAT, "tasks": tasks}, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(
            prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX, dir=self.directory
        )
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        _sync_directory(self.directory)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _sync_directory(directory: str) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = [
    "DIRECTORY",
    "INDEX_FILE",
    "MAX_ENTRIES",
    "Entries",
    "Link",
    "TaskIndex",
    "block_of",
]
