# SPDX-License-Identifier: Apache-2.0
"""The note writer: only owned files and blocks, atomic, compare-and-swap (#3, threat T30).

`after` bytes here are what the core's `horae.apply.notes.note_after` computes for the intent;
the sink only writes them, and refuses those that would touch what Horae does not own."""

import asyncio
import errno
import hashlib
import os
import stat
import threading
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
    with pytest.raises(SinkError, match="note and task intents only"):
        asyncio.run(sink.apply(ApplyRequest(paths(vault), {"type": "http.post"})))
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
    assert refused(vault, create(), b"x\n", None) == "note_exists"  # `os.link`: no clobber
    assert (vault / OWNED).read_bytes() == b"theirs\n"
    assert leftovers(vault / "Daily") == []


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
    found = asyncio.run(VaultSink([TOOL]).identity(paths(vault)))
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


# --- undo of several blocks; markers that do not explain the change ----------------------------

OTHER = BLOCK.replace("brief", "other")
NEW_OTHER = NEW_BLOCK.replace("brief", "other")


def test_undo_of_two_blocks_replaced_in_one_user_note(vault: Path) -> None:
    original = put(vault, USER, f"# Mine\n{BLOCK}middle\n{OTHER}tail\n".encode())
    now = f"# Mine\n{NEW_BLOCK}middle\n{NEW_OTHER}tail\n".encode()
    apply(vault, replace(), f"# Mine\n{NEW_BLOCK}middle\n{OTHER}tail\n".encode(), sha(original))
    apply(vault, replace(block="other"), now, sha((vault / USER).read_bytes()))
    assert (vault / USER).read_bytes() == now
    restore(vault, USER, original, sha(now))
    assert (vault / USER).read_bytes() == original


def test_undo_of_one_block_replaced_twice(vault: Path) -> None:
    original = put(vault, USER, f"# Mine\n{BLOCK}tail\n".encode())
    first = f"# Mine\n{NEW_BLOCK}tail\n".encode()
    second = first.replace(b"new\n", b"newer\nlines\n")
    apply(vault, replace(), first, sha(original))
    apply(vault, replace(), second, sha(first))
    restore(vault, USER, original, sha(second))
    assert (vault / USER).read_bytes() == original


def test_undo_of_a_run_that_changed_a_user_block_and_an_owned_note(vault: Path) -> None:
    user = put(vault, USER, f"# Mine\n{BLOCK}{OTHER}".encode())
    owned = put(vault, OWNED, b"# Day\n")
    user_after = f"# Mine\n{NEW_BLOCK}{NEW_OTHER}".encode()
    owned_after = b"# Day\n\n## H\n\nx\n"
    put(vault, USER, user_after)
    put(vault, OWNED, owned_after)
    created = put(vault, "Daily/new.assistant.md", b"x\n")
    restore(vault, "Daily/new.assistant.md", None, sha(created))
    restore(vault, OWNED, owned, sha(owned_after))
    restore(vault, USER, user, sha(user_after))
    assert (vault / USER).read_bytes() == user
    assert (vault / OWNED).read_bytes() == owned
    assert not (vault / "Daily/new.assistant.md").exists()


@pytest.mark.parametrize(
    "noise",
    [
        "I wrote horae:end brief in prose.\n",  # a malformed marker of the same block
        "<!-- horae:begin draft -->\n",  # an unpaired block
        "`horae:begin` is how a block starts\n",  # a marker with no name
    ],
)
def test_unrelated_markers_do_not_stop_an_undo(vault: Path, noise: str) -> None:
    original = put(vault, USER, f"# Mine\n{noise}{OTHER}tail\n".encode())
    now = put(vault, USER, f"# Mine\n{noise}{NEW_OTHER}tail\n".encode())
    restore(vault, USER, original, sha(now))
    assert (vault / USER).read_bytes() == original


def test_an_undo_the_blocks_do_not_explain_is_refused(vault: Path) -> None:
    original = f"# Mine\n<!-- horae:begin brief -->\nold\n{OTHER}".encode()
    now = put(vault, USER, f"# Mine\n<!-- horae:begin brief -->\nnew\n{NEW_OTHER}".encode())
    with pytest.raises(SinkError) as raised:  # `brief` has no end: the change is not explained
        restore(vault, USER, original, sha(now))
    assert raised.value.kind == "block_ambiguous"
    plain = put(vault, USER, f"# Mine\n{NEW_OTHER}".encode())
    with pytest.raises(SinkError) as raised:
        restore(vault, USER, f"# Edited\n{OTHER}".encode(), sha(plain))
    assert raised.value.kind == "not_owned"
    nested = f"{NEW_OTHER[:-25]}<!-- horae:begin x -->\n{NEW_OTHER[-25:]}"
    put(vault, USER, nested.encode())
    with pytest.raises(SinkError) as raised:  # the block holds another marker
        restore(vault, USER, OTHER.encode(), sha(nested.encode()))
    assert raised.value.kind == "block_ambiguous"


def test_a_user_note_edited_outside_its_blocks_is_not_restored_even_under_force(
    vault: Path,
) -> None:
    """`horae undo --force` restores the whole note through `restore`: refused here (README,
    "manual repair"), the sink having no `force` flag yet."""
    original = f"# Mine\n{BLOCK}".encode()
    now = put(vault, USER, f"# Mine, edited\n{NEW_BLOCK}".encode())
    with pytest.raises(SinkError) as raised:
        restore(vault, USER, original, sha(now))
    assert raised.value.kind == "not_owned"
    assert (vault / USER).read_bytes() == now


# --- owned notes: the block and the rest of the note ------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        (b"# Day\n" + BLOCK.encode(), b"# EDITED\n" + NEW_BLOCK.encode(), "not_owned"),
        (b"# Day\n", NEW_BLOCK.encode() + b"# Day\n", "not_owned"),  # not after the note
        (b"# Day\n", b"# Day\n" + (NEW_BLOCK * 2).encode(), "block_ambiguous"),
        (b"# Day\n", b"# Day\nno block\n", "block_ambiguous"),
        (None, NEW_BLOCK.replace("new", "<!-- horae:begin x -->").encode(), "block_ambiguous"),
    ],
)
def test_a_replaced_block_of_an_owned_note_keeps_the_rest(
    vault: Path, before: bytes | None, after: bytes, kind: str
) -> None:
    if before is not None:
        put(vault, OWNED, before)
    assert refused(vault, replace(OWNED), after, sha(before)) == kind


@pytest.mark.parametrize("block", ["Brief", "-x", "a" * 65, "a b", ""])
def test_the_block_id_is_the_cores(vault: Path, block: str) -> None:
    before = put(vault, USER, BLOCK.encode())
    assert refused(vault, replace(block=block), NEW_BLOCK.encode(), sha(before)) == (
        "intent_invalid"
    )


# --- paths ---------------------------------------------------------------------------------------


def test_lone_surrogates_and_long_utf8_segments_are_refused(vault: Path) -> None:
    for path in ("Notes/\udcff.assistant.md", "\u00e9" * 122 + ".assistant.md"):
        assert refused(vault, create(path), b"x\n", None) == "path_invalid"
    fits = "\u00e9" * 121 + ".assistant.md"  # 255 bytes
    apply(vault, create(fits), b"x\n")
    assert (vault / fits).read_bytes() == b"x\n"


def test_a_relative_vault_is_a_configuration_error(vault: Path) -> None:
    with pytest.raises(SinkError, match="must be an absolute path") as raised:
        writer.read(Paths(write=("vault",)), OWNED)
    assert raised.value.kind == "config_invalid"


def test_a_collision_names_the_name_on_disk(vault: Path) -> None:
    put(vault, "Daily/Today\u202e.Assistant.md", b"# Mine\n")
    with pytest.raises(SinkError) as raised:
        writer.read(paths(vault), "Daily/today\u202e.assistant.md")
    assert raised.value.kind == "path_collision"
    assert '"Today\\u202e.Assistant.md"' in raised.value.message
    assert "\u202e" not in raised.value.message


def test_lookup_compares_ascii_and_other_names_alike(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = ["k.md", "Caf\u00e9.md", "plain.md"]
    monkeypatch.setattr(os, "listdir", lambda fd: listing)
    assert writer._lookup(0, "plain.md", "p")
    assert not writer._lookup(0, "new.md", "p")
    for name in ("\u212a.md", "K.md", "cafe\u0301.md", "CAF\u00c9.md"):  # Kelvin sign, NFD
        with pytest.raises(SinkError) as raised:
            writer._lookup(0, name, "p")
        assert raised.value.kind == "path_collision", name
    listing[:] = ["\u212a.md"]
    with pytest.raises(SinkError):
        writer._lookup(0, "k.md", "p")


def test_a_large_directory_stays_fast(vault: Path) -> None:
    crowd = vault / "Crowd"
    crowd.mkdir()
    for number in range(10_000):
        (crowd / f"note-{number:05d}.md").touch()
    (crowd / "Caf\u00e9-\u00e0.md").touch()
    started = time.perf_counter()
    for number in range(5):
        apply(vault, create(f"Crowd/n{number}.assistant.md"), b"x\n")
        writer.read(paths(vault), f"Crowd/n{number}.assistant.md")
    assert time.perf_counter() - started < 2.0


# --- writes: link, re-read, stale files, faults, concurrency -----------------------------------


def test_a_new_note_is_linked_in_place_and_never_clobbers(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked: list[tuple[str, str]] = []
    real_link = os.link

    def spy(src: Any, dst: Any, **kwargs: Any) -> None:
        linked.append((str(src), str(dst)))
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", spy)
    apply(vault, create(), b"x\n")
    ((src, dst),) = linked
    assert src.startswith(".horae-")
    assert dst == "2026-10-29.assistant.md"
    assert (vault / OWNED).stat().st_nlink == 1
    assert leftovers(vault / "Daily") == []


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV])
def test_a_file_system_without_hard_links_falls_back_to_rename(
    vault: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def no_link(*args: Any, **kwargs: Any) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "link", no_link)
    apply(vault, create(), b"x\n")
    assert (vault / OWNED).read_bytes() == b"x\n"
    assert leftovers(vault / "Daily") == []


def test_a_failing_link_is_unwritable(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "link", broken)
    assert refused(vault, create(), b"x\n", None) == "unwritable"
    assert not (vault / OWNED).exists()
    assert leftovers(vault / "Daily") == []


def test_a_same_size_edit_within_the_clock_tick_is_caught(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = put(vault, OWNED, b"# Day\n")
    info = (vault / OWNED).stat()
    real_sweep = writer._sweep

    def edit_in_place(dir_fd: int) -> None:
        with open(vault / OWNED, "r+b") as note:  # same inode, same size
            note.write(b"# Dax\n")
        os.utime(vault / OWNED, ns=(info.st_atime_ns, info.st_mtime_ns))
        real_sweep(dir_fd)

    monkeypatch.setattr(writer, "_sweep", edit_in_place)
    assert refused(vault, append(), before + b"\n## H\n\nx\n", sha(before)) == "target_changed"
    assert (vault / OWNED).read_bytes() == b"# Dax\n"
    assert leftovers(vault / "Daily") == []


def test_stale_old_files_are_swept_only_when_linked_elsewhere(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daily = vault / "Daily"
    note = put(vault, "Daily/kept.md", b"note\n")
    linked, alone = daily / ".horae-00000000000a.old", daily / ".horae-00000000000b.old"
    os.link(daily / "kept.md", linked)
    alone.write_bytes(b"the only copy\n")
    monkeypatch.setattr(writer, "STALE_TMP_S", -60.0)  # everything counts as stale
    apply(vault, create(), b"x\n")
    assert leftovers(daily) == [alone.name]
    assert (daily / "kept.md").read_bytes() == note
    assert (daily / "kept.md").stat().st_nlink == 1


def test_a_put_back_whose_unlink_fails_still_reports_and_keeps_the_note(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(vault, OWNED, b"theirs\n")
    real_unlink = os.unlink

    def no_unlink(name: Any, **kwargs: Any) -> None:
        if str(name).endswith(".old"):
            raise PermissionError(13, "Permission denied")
        real_unlink(name, **kwargs)

    monkeypatch.setattr(os, "unlink", no_unlink)
    with pytest.raises(SinkError) as raised:
        restore(vault, OWNED, None, sha(b"what Horae wrote\n"))  # CAS passes: see below
    assert raised.value.kind == "target_changed"
    assert (vault / OWNED).read_bytes() == b"theirs\n"


def test_a_failed_put_back_keeps_the_bytes_under_the_old_name(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(vault, OWNED, b"mine\n")
    real_rename = os.rename

    def swapped(src: Any, dst: Any, **kwargs: Any) -> None:
        (vault / OWNED).write_bytes(b"swapped\n")
        real_rename(src, dst, **kwargs)

    def no_link(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "rename", swapped)
    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(SinkError, match=r"kept as \.horae-[0-9a-f]{12}\.old") as raised:
        restore(vault, OWNED, None, sha(b"mine\n"))
    assert raised.value.kind == "target_changed"
    (kept,) = leftovers(vault / "Daily")
    assert (vault / "Daily" / kept).read_bytes() == b"swapped\n"


def test_a_failing_close_of_the_temporary_file_leaves_the_note_whole(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = put(vault, OWNED, b"# Day\n")
    real_open, real_close = os.open, os.close
    temporary: set[int] = set()

    def spy_open(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(name, flags, *args, **kwargs)
        if str(name).endswith(".tmp"):
            temporary.add(fd)
        return fd

    def failing_close(fd: int) -> None:
        real_close(fd)
        if fd in temporary:
            temporary.discard(fd)
            raise OSError(errno.ENOSPC, "No space left on device")  # NFS reports it on close

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", failing_close)
    assert refused(vault, append(), before + b"\n## H\n\nx\n", sha(before)) == "unwritable"
    assert (vault / OWNED).read_bytes() == before
    assert leftovers(vault / "Daily") == []


def test_an_interrupted_write_removes_its_temporary_file(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = put(vault, OWNED, b"# Day\n")

    def interrupted(fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        apply(vault, append(), before + b"\n## H\n\nx\n", sha(before))
    assert (vault / OWNED).read_bytes() == before
    assert leftovers(vault / "Daily") == []


def test_a_directory_that_cannot_be_made_is_unwritable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_mkdir(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "mkdir", no_mkdir)
    assert refused(vault, create("New/a.assistant.md"), b"x\n", None) == "unwritable"
    assert not (vault / "New").exists()


def test_two_concurrent_writes_of_one_note_one_wins(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = put(vault, USER, f"# Mine\n{BLOCK}".encode())
    real_sweep = writer._sweep
    both_read = threading.Barrier(2, timeout=10)

    def after_both_read(dir_fd: int) -> None:
        both_read.wait()
        real_sweep(dir_fd)

    monkeypatch.setattr(writer, "_sweep", after_both_read)
    outcomes: dict[str, str] = {}

    def write(text: str) -> None:
        after = f"# Mine\n{NEW_BLOCK.replace('new', text)}".encode()
        try:
            apply(vault, replace(), after, sha(before))
            outcomes[text] = "wrote"
        except SinkError as error:
            outcomes[text] = error.kind

    threads = [threading.Thread(target=write, args=(text,)) for text in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes.values()) == ["target_changed", "wrote"]
    (winner,) = (text for text, outcome in outcomes.items() if outcome == "wrote")
    assert (vault / USER).read_bytes() == f"# Mine\n{NEW_BLOCK.replace('new', winner)}".encode()
    assert leftovers(vault / "Notes") == []
