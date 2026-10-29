# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a fake inbox answering from synthetic data (never real messages)."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from horae_sdk import Plugin

from horae_obsidian import build

HERE = Path(__file__).parent


class FakeInbox:
    """Answers from `tests/fixtures/inbox.json`: folder name to messages."""

    def __init__(self, path: Path) -> None:
        self.folders: dict[str, list[dict[str, Any]]] = json.loads(path.read_text("utf-8"))

    def messages(self, folder: str) -> Sequence[Mapping[str, Any]]:
        return self.folders.get(folder, [])


@pytest.fixture
def plugin() -> Plugin:
    return build(FakeInbox(HERE / "fixtures" / "inbox.json"))
