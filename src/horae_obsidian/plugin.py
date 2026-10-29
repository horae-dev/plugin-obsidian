# SPDX-License-Identifier: Apache-2.0
"""The plugin: reads and writes an Obsidian vault in Obsidian Tasks syntax.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_obsidian`). It registers no tool yet: this version ships the Tasks parser
(`horae_obsidian.tasks`) and the safe vault reader (`horae_obsidian.vault`) that the
`obsidian.open_tasks` source and the `task.update_status` sink (#4) build on. The plugin will
never write anything itself outside its sinks: it returns data and proposes intents, which
the core checks against `plugin.yaml`'s permissions, shows as a dry-run diff and applies.
"""

from typing import Any

from horae_sdk import Permissions, Plugin

NAME = "obsidian"
"""The plugin's name, also the namespace of its refs (`obsidian.open_tasks`) and
fingerprints (`obsidian:id:…`)."""
VERSION = "0.1.0"
COMMAND = ["python3", "-m", "horae_obsidian"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions()
"""Nothing yet: the vault path and the intents come with the tools that use them (#3, #4)."""


def build() -> Plugin:
    """The plugin (no tool registered yet)."""
    return Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Reads and writes an Obsidian vault in Obsidian Tasks syntax.",
    )


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_obsidian --manifest`)."""
    return build().manifest(command=COMMAND, license="Apache-2.0")
