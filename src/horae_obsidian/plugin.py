# SPDX-License-Identifier: Apache-2.0
"""The plugin: reads and writes an Obsidian vault in Obsidian Tasks syntax.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_obsidian`). It provides the `obsidian.write_note` sink (`horae_obsidian.sink`, #3): the
core's bytes for `note.*` intents, written into owned files and blocks only. The Tasks parser
(`horae_obsidian.tasks`) and the safe vault reader (`horae_obsidian.vault`) are what the
`obsidian.open_tasks` source and the `task.update_status` sink (#4) build on. The plugin
never writes anything outside its sinks: it returns data and proposes intents, which the core
checks against `plugin.yaml`'s permissions, shows as a dry-run diff and applies.
"""

from typing import Any

from horae_sdk import Permissions, Plugin

from horae_obsidian.sink import TOOL, VaultSink
from horae_obsidian.writer import NOTE_TYPES

NAME = "obsidian"
"""The plugin's name, also the namespace of its refs (`obsidian.open_tasks`) and
fingerprints (`obsidian:id:…`)."""
VERSION = "0.1.0"
COMMAND = ["python3", "-m", "horae_obsidian"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(intents=list(NOTE_TYPES))
"""The note intents `obsidian.write_note` applies. No path: where the vault is is the user's
choice, granted as the single `paths.write` entry in `horae.lock` (as for the core's
`file-sink`); the core passes it with every sink operation."""


def build() -> Plugin:
    """The plugin, with the `obsidian.write_note` sink."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Reads and writes an Obsidian vault in Obsidian Tasks syntax.",
    )
    plugin.add(VaultSink([TOOL]))
    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_obsidian --manifest`)."""
    return build().manifest(command=COMMAND, license="Apache-2.0")
