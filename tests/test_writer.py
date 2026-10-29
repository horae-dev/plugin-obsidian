# SPDX-License-Identifier: Apache-2.0
"""The note writer: only owned files and blocks, atomic, compare-and-swap (#3, threat T30).

`after` bytes here are what the core's `horae.apply.notes.note_after` computes for the intent;
the sink only writes them, and refuses those that would touch what Horae does not own."""

import asyncio
import hashlib
import os
import stat
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from horae_sdk import ApplyRequest, Paths, SinkError

import horae_obsidian.writer as writer
from horae_obsidian.sink import TOOL, VaultSink

OWNED = "Daily/2026-10-29.assistant.md"
USER = "Notes/user.md"
BLOCK = "<!-- horae:begin brief -->\nold\n<!-- horae:end brief -->\n"
NEW_BLOCK = "<!-- horae:begin brief -->\nnew\n<!-- horae:end brief -->\n"


def sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Daily").mkdir(parents=True)
    (root / "Notes").mkdir()
    return root


def paths(vault: Path) -> Paths:
    return Paths(write=(str(vault),))


def apply(
    vault: Path,
    intent: dict[str, Any],
    content: bytes,
    expected: str | None = None,
    *,
    sink_paths: Paths | None = None,
) -> dict[str, Any]:
    request = ApplyRequest(
        sink_paths or paths(vault), intent, content=content, expected_before_sha256=expected
    )
    return asyncio.run(VaultSink([TOOL]).apply(request)).to_wire()


def refused(vault: Path, intent: dict[str, Any], content: bytes, expected: str | None) -> str:
    with pytest.raises(SinkError) as raised:
        apply(vault, intent, content, expected)
    return raised.value.kind


def create(path: str = OWNED) -> dict[str, Any]:
    return {"type": "note.create", "path": path, "content": "x"}


def append(path: str = OWNED) -> dict[str, Any]:
    return {"type": "note.append_section", "path": path, "heading": "H", "content": "x"}


def replace(path: str = USER, block: str = "brief") -> dict[str, Any]:
    return {"type": "note.replace_owned_block", "path": path, "block": block, "content": "new"}


def put(vault: Path, path: str, data: bytes) -> bytes:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return data


def leftovers(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.startswith(".horae-"))


# --- owned files ------------------------------------------------------------------------------


def test_creates_an_owned_note_atomically_with_its_directories(vault: Path) -> None:
    result = apply(vault, create("New/Deep/a.assistant.md"), b"# Brief\n")
    target = vault / "New/Deep/a.assistant.md"
    assert target.read_bytes() == b"# Brief\n"
    assert result == {"target": "New/Deep/a.assistant.md", "after_sha256": sha(b"# Brief\n")}
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert leftovers(target.parent) == []


def test_note_create_refuses_an_unowned_note_and_an_existing_one(vault: Path) -> None:
    assert refused(vault, create(USER), b"x\n", None) == "not_owned"
    assert not (vault / USER).exists()
    before = put(vault, OWNED, b"mine\n")
    assert refused(vault, create(), b"x\n", None) == "note_exists"
    assert (vault / OWNED).read_bytes() == before


def test_appends_only_to_an_existing_owned_note(vault: Path) -> None:
    before = put(vault, OWNED, b"# Day\n")
    after = b"# Day\n\n## H\n\nx\n"
    apply(vault, append(), after, sha(before))
    assert (vault / OWNED).read_bytes() == after
    user = put(vault, USER, b"# Mine\n")
    assert refused(vault, append(USER), user + b"\n## H\n\nx\n", sha(user)) == "not_owned"
    assert (vault / USER).read_bytes() == user
    assert refused(vault, append("Daily/missing.assistant.md"), b"x\n", None) == "note_missing"


def test_an_append_that_rewrites_the_note_is_refused(vault: Path) -> None:
    before = put(vault, OWNED, b"# Day\nkeep\n")
    assert refused(vault, append(), b"# Day\n\n## H\n\nx\n", sha(before)) == "not_owned"
    assert (vault / OWNED).read_bytes() == before


def test_ownership_is_the_exact_suffix(vault: Path) -> None:
    for path in ("Daily/a.Assistant.md", "Daily/assistant.md", "Daily/a.assistant.md.md"):
        assert refused(vault, create(path), b"x\n", None) == "not_owned"
    assert writer.is_owned("a.assistant.md")
    assert writer.is_owned("Deep/x.assistant.md")


def test_an_owned_note_takes_a_new_block_anywhere(vault: Path) -> None:
    before = put(vault, OWNED, b"# Day\n")
    after = b"# Day\n\n" + NEW_BLOCK.encode()
    apply(vault, replace(OWNED), after, sha(before))
    assert (vault / OWNED).read_bytes() == after
    apply(vault, replace("Daily/fresh.assistant.md"), NEW_BLOCK.encode(), None)


# --- owned blocks in a user's note ------------------------------------------------------------


def test_replaces_only_the_owned_block_of_a_user_note(vault: Path) -> None:
    before = put(vault, USER, f"# Mine\n{BLOCK}tail\n".encode())
    after = f"# Mine\n{NEW_BLOCK}tail\n".encode()
    apply(vault, replace(), after, sha(before))
    assert (vault / USER).read_bytes() == after


def test_crlf_block_lines_are_recognised(vault: Path) -> None:
    before = put(vault, USER, BLOCK.replace("\n", "\r\n").encode())
    after = NEW_BLOCK.replace("\n", "\r\n").encode()
    apply(vault, replace(), after, sha(before))
    assert (vault / USER).read_bytes() == after


@pytest.mark.parametrize(
    "after",
    [
        f"# EDITED\n{NEW_BLOCK}tail\n",  # before the block
        f"# Mine\n{NEW_BLOCK}tail changed\n",  # after it
        f"# Mine\n{NEW_BLOCK}tail\nmore\n",  # appended past it
        "# Mine\ntail\n",  # the block removed
        f"# Mine\n{NEW_BLOCK.replace('brief', 'other')}tail\n",  # another block
    ],
)
def test_a_change_outside_the_block_of_a_user_note_is_refused(vault: Path, after: str) -> None:
    before = put(vault, USER, f"# Mine\n{BLOCK}tail\n".encode())
    assert refused(vault, replace(), after.encode(), sha(before)) == "not_owned"
    assert (vault / USER).read_bytes() == before


def test_a_user_note_without_the_block_is_not_owned(vault: Path) -> None:
    before = put(vault, USER, b"# Mine\n")
    assert refused(vault, replace(), b"# Mine\n\n" + NEW_BLOCK.encode(), sha(before)) == (
        "not_owned"
    )
    assert refused(vault, replace("Notes/none.md"), NEW_BLOCK.encode(), None) == "not_owned"


@pytest.mark.parametrize(
    ("note", "why"),
    [
        (BLOCK + BLOCK, "duplicated"),
        (
            "<!-- horae:begin brief -->\n<!-- horae:begin brief -->\nx\n"
            "<!-- horae:end brief -->\n<!-- horae:end brief -->\n",
            "nested",
        ),
        ("<!-- horae:begin brief -->\nold\n", "no end"),
        ("old\n<!-- horae:end brief -->\n", "no begin"),
        ("<!-- horae:end brief -->\nold\n<!-- horae:begin brief -->\n", "reversed"),
        ("<!-- horae:begin brief --> trailing\nold\n<!-- horae:end brief -->\n", "malformed"),
        ("<!-- HORAE:BEGIN brief -->\nold\n<!-- horae:end brief -->\n", "not canonical"),
        ("<!-- horae:begin brief --><!-- horae:end brief -->\n", "two on one line"),
        (f"```markdown\n{BLOCK}```\n{BLOCK}", "spoofed in a code fence"),
    ],
)
def test_ambiguous_markers_are_refused(vault: Path, note: str, why: str) -> None:
    before = put(vault, USER, note.encode())
    after = (note + "x\n").encode()  # whatever the bytes, the markers decide first
    assert refused(vault, replace(), after, sha(before)) == "block_ambiguous", why
    assert (vault / USER).read_bytes() == before


def test_a_new_block_cannot_hold_another_marker(vault: Path) -> None:
    before = put(vault, USER, BLOCK.encode())
    after = b"<!-- horae:begin brief -->\n<!-- horae:begin x -->\n<!-- horae:end brief -->\n"
    assert refused(vault, replace(), after, sha(before)) == "block_ambiguous"


def test_a_user_note_that_is_not_utf8_is_refused(vault: Path) -> None:
    before = put(vault, USER, BLOCK.encode() + b"\xff\n")
    assert refused(vault, replace(), NEW_BLOCK.encode() + b"\xff\n", sha(before)) == (
        "note_not_text"
    )


def test_other_intents_and_missing_bytes_are_refused(vault: Path) -> None:
    sink = VaultSink([TOOL])
    with pytest.raises(SinkError, match="note intents only"):
        asyncio.run(sink.apply(ApplyRequest(paths(vault), {"type": "task.create"})))
    with pytest.raises(SinkError) as raised:
        asyncio.run(sink.apply(ApplyRequest(paths(vault), create())))
    assert raised.value.kind == "content_missing"
    assert refused(vault, {**replace(), "block": None}, b"x\n", None) == "intent_invalid"
    with pytest.raises(SinkError) as raised:
        writer.check_apply({"type": "nope", "path": OWNED}, None, b"")
    assert raised.value.kind == "not_supported"


# --- paths --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escape.assistant.md",
        "Daily/../../escape.assistant.md",
        "/etc/passwd.assistant.md",
        "./a.assistant.md",
        "Daily//a.assistant.md",
        ".obsidian/workspace.assistant.md",
        "Daily/.hidden.assistant.md",
        ".trash/a.assistant.md",
        "Daily\\a.assistant.md",
        "C:/a.assistant.md",
        "Daily/a\x00.assistant.md",
        "Daily/a\n.assistant.md",
        "Daily/a.assistant.txt",
        "",
        "a" * 256 + ".assistant.md",
        "a/" * 600 + "x.assistant.md",
    ],
)
def test_paths_outside_the_vault_or_hidden_are_refused(vault: Path, path: str) -> None:
    assert refused(vault, create(path), b"x\n", None) == "path_invalid"
    with pytest.raises(SinkError, match="not a note path"):
        writer.read(paths(vault), path)


def test_a_path_that_is_not_a_string_is_refused(vault: Path) -> None:
    with pytest.raises(SinkError) as raised:
        writer.note_parts(None)
    assert raised.value.kind == "path_invalid"


def test_the_vault_is_the_single_write_entry(vault: Path) -> None:
    for given, kind in ((Paths(), "vault_unset"), (Paths(write=("/a", "/b")), "config_invalid")):
        with pytest.raises(SinkError) as raised:
            apply(vault, create(), b"x\n", sink_paths=given)
        assert raised.value.kind == kind
    with pytest.raises(SinkError) as raised:
        writer.read(Paths(read=(str(vault),)), OWNED)
    assert raised.value.kind == "vault_unset"


def test_the_target_must_be_allowed_by_the_approved_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Paths, "allowed", lambda self, path, write=False: None)
    assert refused(vault, create(), b"x\n", None) == "outside_vault"


def test_a_missing_vault_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(SinkError) as raised:
        writer.read(Paths(write=(str(tmp_path / "nope"),)), OWNED)
    assert raised.value.kind == "unreadable"


def test_a_symlinked_note_is_never_written_through(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"secret\n")
    (vault / OWNED).symlink_to(outside)
    # `Paths.allowed` resolves the link and finds it outside; `O_NOFOLLOW` refuses it anyway.
    kind = refused(vault, append(), b"secret\n\n## H\n\nx\n", sha(b"secret\n"))
    assert kind == "outside_vault"
    assert outside.read_bytes() == b"secret\n"
    with pytest.raises(SinkError, match="not inside the approved vault"):
        writer.read(paths(vault), OWNED)


def test_a_symlinked_directory_is_never_followed_even_inside_the_vault(
    vault: Path, tmp_path: Path
) -> None:
    (tmp_path / "elsewhere").mkdir()
    (vault / "Out").symlink_to(tmp_path / "elsewhere")
    (vault / "Loop").symlink_to(vault / "Daily")
    for path, kind in (("Out/a.assistant.md", "outside_vault"), ("Loop/a.assistant.md", "symlink")):
        assert refused(vault, create(path), b"x\n", None) == kind
        with pytest.raises(SinkError) as raised:
            writer.parent_exists(paths(vault), path)
        assert raised.value.kind == kind
    assert os.listdir(tmp_path / "elsewhere") == []
    assert sorted(os.listdir(vault / "Daily")) == []


def test_a_symlink_to_a_user_note_cannot_make_it_owned(vault: Path) -> None:
    user = put(vault, USER, b"# Mine\n")
    (vault / OWNED).symlink_to(vault / USER)
    assert refused(vault, append(), user + b"\n## H\n\nx\n", sha(user)) == "symlink"
    assert (vault / USER).read_bytes() == user


def test_hard_links_and_special_files_are_refused(vault: Path) -> None:
    user = put(vault, USER, b"# Mine\n")
    os.link(vault / USER, vault / OWNED)
    assert refused(vault, append(), user + b"\n## H\n\nx\n", sha(user)) == "hard_link"
    os.mkfifo(vault / "Daily/fifo.assistant.md")
    with pytest.raises(SinkError) as raised:
        writer.read(paths(vault), "Daily/fifo.assistant.md")
    assert raised.value.kind == "not_a_file"
    (vault / "Daily/dir.assistant.md").mkdir()
    with pytest.raises(SinkError) as raised:
        writer.read(paths(vault), "Daily/dir.assistant.md")
    assert raised.value.kind == "not_a_file"
    put(vault, "Flat", b"a file\n")
    with pytest.raises(SinkError) as raised:
        writer.read(paths(vault), "Flat/a.assistant.md")
    assert raised.value.kind == "not_a_file"


def test_case_variants_are_collisions(vault: Path) -> None:
    """On a case-insensitive file system `today.assistant.md` would open the user's
    `Today.Assistant.md`; on any other, the synced vault would hold both. Never written."""
    user = put(vault, "Daily/Today.Assistant.md", b"# Mine\n")
    for path in ("Daily/today.assistant.md", "daily/Today.Assistant.md"):
        assert refused(vault, create(path), b"x\n", None) == "path_collision"
        assert refused(vault, append(path), user + b"\n## H\n\nx\n", sha(user)) == (
            "path_collision"
        )
    assert (vault / "Daily/Today.Assistant.md").read_bytes() == user
    assert sorted(os.listdir(vault)) == ["Daily", "Notes"]


def test_unicode_normalisation_variants_are_collisions(vault: Path) -> None:
    nfd = unicodedata.normalize("NFD", "Café")
    nfc = unicodedata.normalize("NFC", "Café")
    put(vault, f"Notes/{nfd}.assistant.md", b"x\n")
    assert refused(vault, create(f"Notes/{nfc}.assistant.md"), b"y\n", None) == "path_collision"
    (vault / nfd).mkdir()
    with pytest.raises(SinkError) as raised:
        writer.parent_exists(paths(vault), f"{nfc}/a.assistant.md")
    assert raised.value.kind == "path_collision"


def test_an_exact_name_with_twins_is_a_collision(vault: Path) -> None:
    put(vault, "Notes/a.md", b"a\n")
    try:
        put(vault, "Notes/A.md", b"A\n")
    except OSError:  # pragma: no cover
        pytest.skip("cannot create twins")
    if len(os.listdir(vault / "Notes")) < 2:
        pytest.skip("a case-insensitive file system holds no twins")
    with pytest.raises(SinkError) as raised:
        writer.read(paths(vault), "Notes/a.md")
    assert raised.value.kind == "path_collision"


# --- compare-and-swap and atomicity -----------------------------------------------------------


def test_a_digest_mismatch_writes_nothing(vault: Path) -> None:
    before = put(vault, OWNED, b"# Day\n")
    assert refused(vault, append(), b"# Day\n\n## H\n\nx\n", sha(b"other")) == "target_changed"
    assert refused(vault, append(), b"x\n", None) == "target_changed"
    assert (vault / OWNED).read_bytes() == before
    assert leftovers(vault / "Daily") == []
    assert refused(vault, create("Brand/New.assistant.md"), b"x\n", sha(b"y")) == ("target_changed")
    assert not (vault / "Brand").exists()


def test_a_note_changed_during_the_write_is_kept(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = put(vault, OWNED, b"# Day\n")
    real_fsync = os.fsync

    def editor_writes_meanwhile(fd: int) -> None:
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            (vault / OWNED).write_bytes(b"# Day, edited by the user\n")

    monkeypatch.setattr(os, "fsync", editor_writes_meanwhile)
    assert refused(vault, append(), b"# Day\n\n## H\n\nx\n", sha(before)) == "target_changed"
    assert (vault / OWNED).read_bytes() == b"# Day, edited by the user\n"
    assert leftovers(vault / "Daily") == []


def test_a_note_created_during_a_create_is_kept(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def created_meanwhile(fd: int) -> None:
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            (vault / OWNED).write_bytes(b"theirs\n")

    monkeypatch.setattr(os, "fsync", created_meanwhile)
    assert refused(vault, create(), b"x\n", None) == "target_changed"
    assert (vault / OWNED).read_bytes() == b"theirs\n"


@pytest.mark.parametrize("fails", ["write", "fsync", "replace", "fchmod"])
def test_a_crash_mid_write_leaves_the_note_whole(
    vault: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    before = put(vault, OWNED, b"# Day\n" * 1000)
    real = getattr(os, fails)
    calls = {"n": 0}

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if fails == "write" and calls["n"] == 1:
            real(args[0], bytes(args[1][:10]))  # a partial write, then the disk fails
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, fails, failing)
    assert refused(vault, append(), before + b"\n## H\n\nx\n", sha(before)) == "unwritable"
    assert (vault / OWNED).read_bytes() == before
    assert leftovers(vault / "Daily") == []


def test_the_temporary_file_is_unwritable(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = os.open

    def no_tmp(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(name).startswith(".horae-"):
            raise PermissionError(13, "Permission denied")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_tmp)
    assert refused(vault, create(), b"x\n", None) == "unwritable"


def test_a_write_goes_through_a_synced_temporary_file_in_the_same_directory(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = put(vault, OWNED, b"# Day\n")
    os.chmod(vault / OWNED, 0o600)
    synced: list[str] = []
    renamed: list[tuple[str, str, int | None, int | None]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def spy_replace(src: Any, dst: Any, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        renamed.append((str(src), str(dst), src_dir_fd, dst_dir_fd))
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    after = b"# Day\n\n## H\n\nx\n"
    apply(vault, append(), after, sha(before))
    assert synced == ["file", "dir"]
    ((src, dst, src_fd, dst_fd),) = renamed
    assert src.startswith(".horae-")
    assert src.endswith(".tmp")
    assert dst == "2026-10-29.assistant.md"
    assert src_fd == dst_fd
    assert (vault / OWNED).read_bytes() == after
    assert stat.S_IMODE((vault / OWNED).stat().st_mode) == 0o600


def test_stale_temporary_files_are_swept_and_fresh_ones_kept(vault: Path) -> None:
    stale, fresh = vault / "Daily/.horae-0123456789ab.tmp", vault / "Daily/.horae-ba9876543210.tmp"
    stale.write_bytes(b"crash")
    fresh.write_bytes(b"in flight")
    old = time.time() - writer.STALE_TMP_S - 10
    os.utime(stale, (old, old))
    apply(vault, create(), b"x\n")
    assert leftovers(vault / "Daily") == [fresh.name]


def test_an_oversized_note_is_refused(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "MAX_NOTE_BYTES", 8)
    assert refused(vault, create(), b"123456789", None) == "note_too_large"
    put(vault, OWNED, b"123456789")
    with pytest.raises(SinkError) as raised:
        writer.read(paths(vault), OWNED)
    assert raised.value.kind == "note_too_large"


# --- read, parent_exists, identity, restore -----------------------------------------------------


def test_read_and_parent_exists_are_side_effect_free(vault: Path) -> None:
    sink = VaultSink([TOOL])
    put(vault, USER, b"# Mine\n")
    assert asyncio.run(sink.read(paths(vault), USER)) == b"# Mine\n"
    assert asyncio.run(sink.read(paths(vault), "Missing/a.md")) is None
    assert asyncio.run(sink.read(paths(vault), "Notes/none.md")) is None
    assert asyncio.run(sink.parent_exists(paths(vault), USER))
    assert not asyncio.run(sink.parent_exists(paths(vault), "Missing/a.md"))
    assert sorted(os.listdir(vault)) == ["Daily", "Notes"]


def test_identity_names_the_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = os.stat(vault)
    found = VaultSink([TOOL]).identity(paths(vault))
    assert found == f"vault:{info.st_dev}:{info.st_ino}:{os.path.realpath(vault)}"
    monkeypatch.setattr(writer, "MAX_IDENTITY", 10)
    assert ":sha256:" in writer.identity(paths(vault))
    with pytest.raises(SinkError) as raised:
        writer.identity(Paths(write=(str(vault / "nope"),)))
    assert raised.value.kind == "unreadable"


def restore(vault: Path, path: str, content: bytes | None, expected: str | None) -> None:
    asyncio.run(VaultSink([TOOL]).restore(paths(vault), path, content, expected))


def test_undo_removes_or_restores_an_owned_note(vault: Path) -> None:
    after = put(vault, OWNED, b"# Day\n\n## H\n\nx\n")
    restore(vault, OWNED, b"# Day\n", sha(after))
    assert (vault / OWNED).read_bytes() == b"# Day\n"
    restore(vault, OWNED, None, sha(b"# Day\n"))
    assert not (vault / OWNED).exists()
    assert leftovers(vault / "Daily") == []
    restore(vault, OWNED, None, None)  # already gone: nothing to do


def test_undo_restores_only_the_block_of_a_user_note(vault: Path) -> None:
    now = put(vault, USER, f"# Mine\n{NEW_BLOCK}".encode())
    restore(vault, USER, f"# Mine\n{BLOCK}".encode(), sha(now))
    assert (vault / USER).read_bytes() == f"# Mine\n{BLOCK}".encode()
    now = (vault / USER).read_bytes()
    for content in (b"# Mine\n", f"# Other\n{NEW_BLOCK}".encode(), None):
        with pytest.raises(SinkError) as raised:
            restore(vault, USER, content, sha(now))
        assert raised.value.kind == "not_owned"
    with pytest.raises(SinkError) as raised:
        restore(vault, "Notes/gone.md", b"x\n", None)
    assert raised.value.kind == "not_owned"
    assert (vault / USER).read_bytes() == now
    restore(vault, USER, now, sha(now))  # unchanged: allowed


def test_undo_of_a_note_that_changed_meanwhile_is_refused(vault: Path) -> None:
    put(vault, OWNED, b"edited\n")
    with pytest.raises(SinkError) as raised:
        restore(vault, OWNED, None, sha(b"what Horae wrote\n"))
    assert raised.value.kind == "target_changed"
    assert (vault / OWNED).read_bytes() == b"edited\n"


def test_a_removal_that_finds_other_bytes_puts_them_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(vault, OWNED, b"mine\n")
    real_rename = os.rename

    def swapped(src: Any, dst: Any, **kwargs: Any) -> None:
        (vault / OWNED).write_bytes(b"swapped\n")  # replaced just before the rename
        real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", swapped)
    with pytest.raises(SinkError) as raised:
        restore(vault, OWNED, None, sha(b"mine\n"))
    assert raised.value.kind == "target_changed"
    assert (vault / OWNED).read_bytes() == b"swapped\n"
    assert leftovers(vault / "Daily") == []


def test_block_span_follows_the_core() -> None:
    assert writer.block_span("a\n" + BLOCK + "b", "brief", "p") == (2, 2 + len(BLOCK))
    assert writer.block_span("no markers\n", "brief", "p") is None
    assert writer.block_span("<!-- horae:begin other -->\n", "brief", "p") is None
    assert writer.begin_marker("x") == "<!-- horae:begin x -->"
    assert writer.end_marker("x") == "<!-- horae:end x -->"
