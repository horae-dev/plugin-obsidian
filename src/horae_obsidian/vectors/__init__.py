# SPDX-License-Identifier: Apache-2.0
"""Golden vectors of the `obsidian.reminders` transform, shipped with the package so that
consumers (pack-reminders' tests) check their expectations against the same cases.

Each vector is `{name, params, input}` and either `intents` (the exact `notify.push`
proposals, in order) with an optional `data` subset, or `error` (the call fails). Regenerate
with `uv run python tests/data/reminder_vectors.py > src/horae_obsidian/vectors/reminders.json`
after a deliberate change of behaviour, and review the diff.
"""

import json
from importlib import resources
from typing import Any


def reminder_vectors() -> list[dict[str, Any]]:
    """The vectors of `reminders.json`."""
    text = resources.files(__name__).joinpath("reminders.json").read_text("utf-8")
    vectors: list[dict[str, Any]] = json.loads(text)
    return vectors


__all__ = ["reminder_vectors"]
