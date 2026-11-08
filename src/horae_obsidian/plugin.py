# SPDX-License-Identifier: Apache-2.0
"""The plugin: reads and writes an Obsidian vault in Obsidian Tasks syntax.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_obsidian`). It provides the `obsidian.open_tasks` source (`horae_obsidian.source`, #4:
the vault's open tasks), the `obsidian.write_note` sink (`horae_obsidian.sink`, #3: the
core's bytes for `note.*` intents, written into owned files and blocks only) and the
`obsidian.write_task` sink (#4: `task.update_status` edits one task line, `task.create`
appends one to an owned note). The plugin never writes anything outside its sinks: it returns
data, and the core checks the intents against `plugin.yaml`'s permissions, shows them as a
dry-run diff and applies them.
"""

from typing import Any

from horae_sdk import Call, Permissions, Plugin

from horae_obsidian import source
from horae_obsidian.sink import TASK_TOOL, TOOL, VaultSink
from horae_obsidian.tasksink import TASK_TYPES
from horae_obsidian.writer import NOTE_TYPES

NAME = "obsidian"
"""The plugin's name, also the namespace of its refs (`obsidian.open_tasks`) and
fingerprints (`obsidian:id:…`)."""
VERSION = "0.1.0"
COMMAND = ["python3", "-m", "horae_obsidian"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(intents=[*NOTE_TYPES, *TASK_TYPES])
"""The note and task intents the sinks apply (the plugin proposes none). No path: where the
vault is is the user's choice, granted as the single `paths.write` entry in `horae.lock` (as
for the core's `file-sink`); the core passes it with every call."""


def build() -> Plugin:
    """The plugin, with the `obsidian.open_tasks` source and the vault sinks."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Reads and writes an Obsidian vault in Obsidian Tasks syntax.",
    )

    @plugin.source(source.REF)
    def open_tasks(call: Call, limit: int = 500) -> dict[str, Any]:
        return source.open_tasks(call.paths, limit=limit)

    plugin.add(VaultSink([TOOL, TASK_TOOL]))
    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_obsidian --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=">=0,<0.2",  # the core is 0.0.x until v0.1; nothing checks it yet
        license="Apache-2.0",
    )
