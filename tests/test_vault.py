# SPDX-License-Identifier: Apache-2.0
"""Reading notes from a vault: never through a symbolic link, never past the size cap."""

import json
import os
import shutil
from pathlib import Path

import pytest

import horae_obsidian.vault as vault_module
from horae_obsidian.tasks import Document
from horae_obsidian.vault import (
    Skipped,
    VaultError,
    iter_scan,
    note_parts,
    read_note,
    safe_path,
    scan,
)

VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    shutil.copytree(VAULT, root)
    return root


def kind(vault: Path, path: str, **kwargs: int) -> str:
    with pytest.raises(VaultError) as raised:
        read_note(vault, path, **kwargs)
    return raised.value.kind


def test_reads_a_note_with_its_exact_bytes(vault: Path) -> None:
    document = read_note(vault, "Projects/crlf.md")
    assert document.data == (VAULT / "Projects" / "crlf.md").read_bytes()
    assert document.path == "Projects/crlf.md"
    assert document.tasks[0].description == "Windows line"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd.md",
        "../x.md",
        "a/../b.md",
        "./a.md",
        "a//b.md",
        ".obsidian/x.md",
        "a/.hidden.md",
        "notes.txt",
        "a\0.md",
        "dir/",
    ],
)
def test_only_note_paths(vault: Path, path: str) -> None:
    assert kind(vault, path) == "invalid_path"


def test_note_parts() -> None:
    assert note_parts("Daily/2026-10-22.md") == ["Daily", "2026-10-22.md"]


def test_symbolic_links_are_never_followed(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.md"
    outside.write_text("- [ ] outside the vault\n")
    (vault / "link.md").symlink_to(outside)
    (vault / "inside.md").symlink_to(vault / "statuses.md")
    (vault / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (vault / "dangling.md").symlink_to(tmp_path / "nothing.md")
    assert kind(vault, "link.md") == "symlink"
    assert kind(vault, "inside.md") == "symlink"
    assert kind(vault, "dangling.md") == "symlink"
    assert kind(vault, "dirlink/secret.md") in {"symlink", "not_a_file"}


def test_not_found_and_not_a_file(vault: Path) -> None:
    assert kind(vault, "missing.md") == "not_found"
    assert kind(vault, "missing/x.md") == "not_found"
    (vault / "folder.md").mkdir()
    assert kind(vault, "folder.md") == "not_a_file"
    assert kind(vault, "statuses.md/x.md") == "not_a_file"
    os.mkfifo(vault / "pipe.md")
    assert kind(vault, "pipe.md") == "not_a_file"


def test_the_size_cap(vault: Path) -> None:
    size = len((vault / "statuses.md").read_bytes())
    assert read_note(vault, "statuses.md", max_bytes=size).data
    assert kind(vault, "statuses.md", max_bytes=size - 1) == "too_large"


def test_scan_reads_every_note_and_skips_the_rest(vault: Path, tmp_path: Path) -> None:
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "hidden.md").write_text("- [ ] hidden\n")
    (vault / ".hidden.md").write_text("- [ ] hidden\n")
    (vault / "link.md").symlink_to(tmp_path / "x.md")
    (vault / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (vault / "big.md").write_bytes(b"x" * 5000)
    result = scan(vault, max_bytes=4096)
    paths = [d.path for d in result.documents]
    expected = sorted(p.relative_to(VAULT).as_posix() for p in VAULT.rglob("*.md"))
    assert paths == expected
    assert result.skipped == (
        Skipped("big.md", "too_large"),
        Skipped("dirlink", "symlink"),
        Skipped("link.md", "symlink"),
    )
    assert not result.truncated
    for document in result.documents:
        assert document.render() == (VAULT / document.path).read_bytes()


def test_scan_stops_at_max_notes(vault: Path) -> None:
    result = scan(vault, max_notes=2)
    assert len(result.documents) == 2
    assert result.truncated


def test_scan_and_read_need_a_vault_directory(tmp_path: Path) -> None:
    with pytest.raises(VaultError) as missing:
        scan(tmp_path / "missing")
    assert missing.value.kind == "not_found"
    (tmp_path / "file").write_text("x")
    with pytest.raises(VaultError) as not_a_directory:
        scan(tmp_path / "file")
    assert not_a_directory.value.kind == "not_a_directory"
    with pytest.raises(VaultError) as read:
        read_note(tmp_path / "missing", "a.md")
    assert read.value.kind == "not_found"


def test_read_note_wraps_os_errors(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(fd: int) -> os.stat_result:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fstat", broken)
    assert kind(vault, "statuses.md") == "unreadable"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_an_unreadable_directory_is_skipped(vault: Path) -> None:
    locked = vault / "locked"
    locked.mkdir()
    (locked / "x.md").write_text("- [ ] x\n")
    locked.chmod(0)
    try:
        result = scan(vault)
    finally:
        locked.chmod(0o700)
    assert Skipped("locked", "unreadable") in result.skipped
    assert len(result.documents) == len(list(VAULT.rglob("*.md")))


def test_scan_depth_cap(vault: Path) -> None:
    deep = vault / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (vault / "a" / "one.md").write_text("- [ ] one\n")
    (deep / "three.md").write_text("- [ ] three\n")
    result = scan(vault, max_depth=2)
    paths = {d.path for d in result.documents}
    assert "a/one.md" in paths
    assert "a/b/c/three.md" not in paths
    assert Skipped("a/b/c", "too_deep") in result.skipped
    assert "a/b/c/three.md" in {d.path for d in scan(vault).documents}


def test_scan_byte_budget(vault: Path) -> None:
    total = sum(len(p.read_bytes()) for p in VAULT.rglob("*.md"))
    assert not scan(vault, max_total_bytes=total).truncated
    result = scan(vault, max_total_bytes=total // 2)
    assert result.truncated
    budget = [s.path for s in result.skipped if s.kind == "budget"]
    assert budget
    assert len(result.documents) + len(budget) == len(list(VAULT.rglob("*.md")))
    assert sum(d.size for d in result.documents) <= total // 2


def test_scan_task_budget(vault: Path) -> None:
    total = sum(len(d.tasks) for d in scan(vault).documents)
    assert not scan(vault, max_total_tasks=total).truncated
    result = scan(vault, max_total_tasks=total - 1)
    assert result.truncated
    assert sum(len(d.tasks) for d in result.documents) <= total - 1
    assert any(s.kind == "budget" for s in result.skipped)


def test_scan_stops_walking_at_max_notes(vault: Path) -> None:
    items = list(iter_scan(vault, max_notes=1))
    assert isinstance(items[0], Document)
    assert items[-1] == Skipped("", "truncated")
    assert sum(isinstance(i, Document) for i in items) == 1


def test_iter_scan_streams_documents_with_their_stat(vault: Path) -> None:
    items = list(iter_scan(vault))
    documents = [i for i in items if isinstance(i, Document)]
    assert {d.path for d in documents} == {d.path for d in scan(vault).documents}
    for document in documents:
        info = (vault / document.path).stat()
        assert (document.mtime_ns, document.ino, document.size) == (
            info.st_mtime_ns,
            info.st_ino,
            info.st_size,
        )


def test_reported_paths_escape_control_and_bidi_characters(vault: Path) -> None:
    (vault / "evil\x1b[31m\u202e.md").symlink_to(vault / "statuses.md")
    expected = {"evil\\x1b[31m\\u202e.md"}
    try:  # Linux filesystems take any byte; APFS refuses invalid UTF-8
        (vault / "bad\udcff.md").symlink_to(vault / "statuses.md")
        expected.add("bad\\xff.md")
    except OSError:
        pass
    paths = {s.path for s in scan(vault).skipped if s.kind == "symlink"}
    assert expected <= paths
    assert all(c.isprintable() for p in paths for c in p)
    assert safe_path("a\\b\u2028\u200b\x7f\udce9") == "a\\\\b\\u2028\\u200b\\x7f\\xe9"
    with pytest.raises(VaultError) as raised:
        read_note(vault, "x\x1b]0;title\x07.md")
    assert "\x1b" not in str(raised.value)
    assert "\\x1b" in str(raised.value)


def test_an_unexpected_error_skips_only_that_note(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = vault_module.parse  # type: ignore[attr-defined]

    def explode(data: bytes, path: str = "", **kwargs: int | None) -> Document:
        if path == "statuses.md":
            raise RuntimeError("secret detail never shown")
        return real(data, path, **kwargs)

    monkeypatch.setattr(vault_module, "parse", explode)
    result = scan(vault)
    assert Skipped("statuses.md", "internal", "RuntimeError") in result.skipped
    assert "statuses.md" not in {d.path for d in result.documents}
    assert all("secret" not in json.dumps(s.to_json()) for s in result.skipped)


def test_duplicate_keys_across_the_vault(vault: Path) -> None:
    (vault / "copy.md").write_text("- [ ] Same id elsewhere 🆔 water-1\n")
    result = scan(vault)
    assert "obsidian:id:water-1" in result.duplicate_keys
    flagged = [
        (d.path, t.key_unique)
        for d in result.documents
        for t in d.tasks
        if t.key == "obsidian:id:water-1"
    ]
    assert flagged == [("copy.md", False), ("ids.md", False)]
    others = [t for d in result.documents for t in d.tasks if t.key not in result.duplicate_keys]
    assert all(t.key_unique for t in others)
    assert scan(VAULT).duplicate_keys == ()


def test_uppercase_suffix_is_not_a_note(vault: Path) -> None:
    (vault / "LOUD.MD").write_text("- [ ] loud\n")
    assert "LOUD.MD" not in {d.path for d in scan(vault).documents}
    assert kind(vault, "LOUD.MD") == "invalid_path"
