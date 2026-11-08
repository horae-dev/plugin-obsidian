# SPDX-License-Identifier: Apache-2.0
"""Which lines are Horae's: its Tasks writer's tasks, its other output, and the user's.

Decisions (maintainer, on #4):

- **Writer notes.** The notes the Tasks writer appends to (`DEFAULT_NOTE`, and any note the
  index records a task or a tombstone in) are **writer-exclusive**: `obsidian.write_note`
  refuses every note intent on them (`tasks_note`), so no LLM-written text reaches them.
  Inside a writer note, a line is **Horae's task** when its `^h-` block id is in the index
  for that note (`Provenance.link`), edited in Obsidian or not (ticked, re-dated, retitled).
- **Horae's output** is never read back as the user's task (`Provenance.hidden`): a line in
  an owned block (`owned_block`), a line of another owned note `*.assistant.md`
  (`owned_note`: LLM-written), and a line of a writer note that is not an indexed task
  (`unverified_line` when it holds a `^h-` block id, e.g. after the index was lost: the next
  `task.create` of its fingerprint adopts it; `owned_note` otherwise).
- **Copies.** A `^h-` line copied into a user note while its writer note still holds the
  task is a plain user task: its key names its own note (`obsidian:block:<path>:h-…`,
  `View.rekey`), so it is neither ambiguous with Horae's line nor found by the fingerprint. A
  line **moved** out of its writer note (the writer note no longer holds it) keeps the link:
  its key stays `obsidian:horae:h-…` and the fingerprint still finds it.
"""

import hashlib
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace

from horae_sdk.intents import fingerprint

from horae_obsidian import writer
from horae_obsidian.index import Entries, Link
from horae_obsidian.tasks import NAME, Document, Task
from horae_obsidian.vault import Scan

DEFAULT_NOTE = "Tasks.assistant.md"
"""The Tasks writer's note when the `act` entry names none (its `note` parameter)."""
_HORAE_KEY = f"{NAME}:horae:"


def same_note(a: str, b: str) -> bool:
    """Whether two note paths name the same note (NFC, case folded: a synced vault reaches
    case-insensitive devices, and the writer refuses twins)."""
    return _norm(a) == _norm(b)


def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


class Provenance:
    """The index, read as "which line is whose"."""

    def __init__(self, entries: Entries | None = None) -> None:
        entries = entries or Entries()
        self.links: dict[str, Link] = entries.links
        paths = [link.path for link in entries.links.values()]
        paths += [tomb.path for tomb in entries.tombstones.values()]
        self.notes = {_norm(DEFAULT_NOTE)} | {_norm(p) for p in paths if p is not None}

    def writer_note(self, path: str) -> bool:
        """Whether the Tasks writer owns the note at `path` (see the module docstring)."""
        return _norm(path) in self.notes

    def link(self, task: Task) -> Link | None:
        """The index entry of `task` when it is Horae's task line in its writer note."""
        link = self.links.get(task.horae_block or "")
        if link is None or link.path is None or task.in_owned_block:
            return None
        return link if same_note(link.path, task.path) else None

    def hidden(self, task: Task) -> str | None:
        """Why `task` is Horae's output, never the user's task (`None`: it is not)."""
        if task.in_owned_block:
            return "owned_block"
        if not writer.is_owned(task.path) or self.link(task) is not None:
            return None
        if self.writer_note(task.path) and task.horae_block is not None:
            return "unverified_line"
        return "owned_note"

    def edited(self, task: Task) -> bool:
        """Whether `task` is Horae's task line, changed since Horae last wrote it."""
        link = self.link(task)
        return link is not None and link.line_sha256 != task.line_sha256


def _block_key(path: str, block: str) -> str:
    """The note-scoped key of a copied `^h-` line (`obsidian:block:<path>:<block>`)."""
    nfc = unicodedata.normalize("NFC", path) or "-"
    try:
        return fingerprint(NAME, "block", nfc, block)
    except Exception:  # a path `fingerprint` refuses: a digest of it
        raw = "\0".join([nfc, block]).encode("utf-8", "surrogatepass")
        return f"{NAME}:block::{hashlib.sha256(raw).hexdigest()[:16]}"


@dataclass(frozen=True)
class View:
    """A scan seen through the index: tasks re-keyed (`rekey`), Horae's output known."""

    scan: Scan
    provenance: Provenance
    held: frozenset[str]
    """The block ids whose writer note holds Horae's task line."""
    counts: Counter[str]
    """How many tasks that are not Horae's output hold each key: a hidden line never makes
    a real task ambiguous (a forged `^h-` copy would otherwise hide the task it copies)."""

    @classmethod
    def of(cls, found: Scan, provenance: Provenance) -> "View":
        held = frozenset(
            task.horae_block
            for document in found.documents
            for task in document.tasks
            if task.horae_block is not None and provenance.link(task) is not None
        )
        view = cls(found, provenance, held, Counter())
        documents = tuple(view.rekey(document) for document in found.documents)
        counts = Counter(
            task.key
            for document in documents
            for task in document.tasks
            if provenance.hidden(task) is None
        )
        return cls(replace(found, documents=documents), provenance, held, counts)

    def rekey(self, document: Document) -> Document:
        """`document` with each copied `^h-` line keyed by its own note (module docstring)."""
        if not any(self._copy(task) for task in document.tasks):
            return document
        tasks = tuple(
            replace(task, key=_block_key(task.path, task.horae_block or ""))
            if self._copy(task)
            else task
            for task in document.tasks
        )
        return replace(document, tasks=tasks)

    def _copy(self, task: Task) -> bool:
        return (
            task.key.startswith(_HORAE_KEY)
            and task.horae_block in self.held
            and self.provenance.hidden(task) is None
            and self.provenance.link(task) is None
        )

    def tasks(self) -> Iterator[tuple[Document, Task]]:
        for document in self.scan.documents:
            for task in document.tasks:
                yield document, task

    def carries(self, task: Task, block: str) -> bool:
        """Whether `task` is the task written for the fingerprint whose block id is `block`:
        its line in the writer note, or, once moved out of it, the moved line."""
        if task.horae_block != block or self.provenance.hidden(task) is not None:
            return False
        return self.provenance.link(task) is not None or block not in self.held

    def source_fingerprint(self, task: Task) -> str | None:
        block = task.horae_block
        if block is None or not self.carries(task, block):
            return None
        link = self.provenance.links.get(block)
        return link.fingerprint if link is not None else None

    def known_unique(self, task: Task) -> bool:
        """`Scan.known_unique`, Horae's output left out of the counts."""
        return self.scan.known_unique(task, self.counts)


__all__ = ["DEFAULT_NOTE", "Provenance", "View", "same_note"]
