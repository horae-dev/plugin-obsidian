# SPDX-License-Identifier: Apache-2.0
"""Regenerate `core_note_vectors.json` from the core's real `horae.apply.notes.note_after`.

    uv run python tests/data/core_vectors.py <core checkout> [<commit>] \\
        > tests/data/core_note_vectors.json

The core's `src/horae/apply/notes.py` is read with `git show <commit>:...` (default
`origin/main`) and run with its two imports stubbed (`SinkError`, the intent classes): nothing
of the core is installed. Each vector is an intent, the note before, what the core makes of it
(`after`, or the `error` kind) and, for a block, the core's `_find_block` span in both texts.
"""

import json
import subprocess
import sys
import types
from dataclasses import dataclass
from typing import Any

NOTES = "src/horae/apply/notes.py"
OWNED = "Daily/d.assistant.md"
USER = "Notes/user.md"
B = "<!-- horae:begin brief -->"
E = "<!-- horae:end brief -->"
OB = "<!-- horae:begin other -->"
OE = "<!-- horae:end other -->"


class SinkError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class NoteCreate:
    path: str
    content: str
    type: str = "note.create"


@dataclass
class NoteAppendSection:
    path: str
    heading: str
    content: str
    type: str = "note.append_section"


@dataclass
class NoteReplaceOwnedBlock:
    path: str
    block: str
    content: str
    type: str = "note.replace_owned_block"


def git(core: str, *args: str) -> str:
    """The output of `git -C <core> <args>` (a developer's own checkout)."""
    command = ["git", "-C", core, *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603


def load(core: str, commit: str) -> types.ModuleType:
    source = git(core, "show", f"{commit}:{NOTES}")
    stubs = {
        "horae": types.ModuleType("horae"),
        "horae.apply": types.ModuleType("horae.apply"),
        "horae.apply.sink": types.ModuleType("horae.apply.sink"),
        "horae.contracts": types.ModuleType("horae.contracts"),
        "horae.contracts.intents": types.ModuleType("horae.contracts.intents"),
    }
    stubs["horae.apply.sink"].SinkError = SinkError  # type: ignore[attr-defined]
    intents = stubs["horae.contracts.intents"]
    intents.NoteCreate = NoteCreate  # type: ignore[attr-defined]
    intents.NoteAppendSection = NoteAppendSection  # type: ignore[attr-defined]
    intents.NoteReplaceOwnedBlock = NoteReplaceOwnedBlock  # type: ignore[attr-defined]
    sys.modules.update(stubs)
    module = types.ModuleType("core_notes")
    exec(compile(source, NOTES, "exec"), module.__dict__)  # noqa: S102
    return module


def blocks(note: str, eol: str = "\n") -> list[tuple[str, str]]:
    """Notes holding blocks, in `eol` line endings."""
    cases = {
        "block in the middle": f"# T\n{B}\nold\n{E}\ntail\n",
        "block at the start": f"{B}\nold\n{E}\ntail\n",
        "block at the end": f"# T\n{B}\nold\n{E}\n",
        "block at the end, no final break": f"# T\n{B}\nold\n{E}",
        "empty block": f"# T\n{B}\n{E}\n",
        "indented markers": f"# T\n  {B}\t\nold\n\t{E}  \ntail\n",
        "two blocks": f"{OB}\nkeep\n{OE}\n{B}\nold\n{E}\n",
        "the block before another": f"{B}\nold\n{E}\nmid\n{OB}\nkeep\n{OE}\n",
        "bom, marker on line 2": f"\ufeff# T\n{B}\nold\n{E}\n",
        "bom, marker on line 1": f"\ufeff{B}\nold\n{E}\n",
        "upper-case marker": f"<!-- HORAE:BEGIN brief -->\nold\n{E}\n",
        "upper-case name": f"<!-- horae:begin BRIEF -->\nold\n{E}\n",
        "duplicated": f"{B}\na\n{E}\n{B}\nb\n{E}\n",
        "no end": f"{B}\nold\n",
        "reversed": f"{E}\nold\n{B}\n",
        "two on one line": f"{B}{E}\n",
        "fenced copy": f"```\n{B}\n{E}\n```\n{B}\nold\n{E}\n",
        "no block": "# T\nbody\n",
        "no block, no final break": "# T\nbody",
        "empty note": "",
    }
    return [(f"{name} ({note}, {eol!r})", text.replace("\n", eol)) for name, text in cases.items()]


def vectors(core: types.ModuleType) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(name: str, intent: Any, before: str | None) -> None:
        vector: dict[str, Any] = {"name": name, "intent": vars(intent), "before": before}
        try:
            after = core.note_after(intent, None if before is None else before.encode())
            vector["after"], vector["error"] = after.decode(), None
        except SinkError as error:
            vector["after"], vector["error"] = None, error.kind
        if isinstance(intent, NoteReplaceOwnedBlock):
            spans: dict[str, Any] = {}
            for side in ("before", "after"):
                text = vector[side]
                if text is None:
                    spans[side] = None
                    continue
                try:
                    spans[side] = core._find_block(text, intent)
                except SinkError as error:
                    spans[side] = error.kind
            vector["spans"] = spans
        out.append(vector)

    add("create", NoteCreate(OWNED, "a\nb"), None)
    add("create, crlf content", NoteCreate(OWNED, "a\r\nb\r\n"), None)
    add("create a user note", NoteCreate(USER, "a"), None)
    for eol in ("\n", "\r\n"):
        for before in ("# T\n", "# T", "\ufeff# T\n", "", "a\nb\n"):
            text = before.replace("\n", eol)
            add(f"append to {text!r}", NoteAppendSection(OWNED, "H", "x\ny"), text)
        for path in (OWNED, USER):
            for name, note in blocks(path, eol):
                for content in ("", "n1\r\nn2\n"):
                    add(
                        f"{name}, content {content!r}",
                        NoteReplaceOwnedBlock(path, "brief", content),
                        note,
                    )
    for path in (OWNED, USER):
        add(f"replace in a missing {path}", NoteReplaceOwnedBlock(path, "brief", "x"), None)
    return out


def main() -> None:
    core = sys.argv[1]
    commit = sys.argv[2] if len(sys.argv) > 2 else "origin/main"
    sha = git(core, "rev-parse", commit).strip()
    header = {
        "comment": "Generated by tests/data/core_vectors.py from the core's note_after",
        "core_commit": sha,
        "core_file": NOTES,
    }
    lines = [json.dumps(vector, ensure_ascii=True) for vector in vectors(load(core, sha))]
    body = ",\n  ".join(lines)
    opening = json.dumps(header, ensure_ascii=True)[:-1]
    sys.stdout.write(f'{opening}, "vectors": [\n  {body}\n]}}\n')


if __name__ == "__main__":
    main()
