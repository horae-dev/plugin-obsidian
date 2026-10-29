# SPDX-License-Identifier: Apache-2.0
"""Reading notes from a vault: never through a symbolic link, never past the size cap."""

import os
import shutil
from pathlib import Path

import pytest

from horae_obsidian.vault import Skipped, VaultError, note_parts, read_note, scan

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
