# SPDX-License-Identifier: Apache-2.0
"""The plugin: reads and writes an Obsidian vault in Obsidian Tasks syntax.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_obsidian`). It provides the `obsidian.open_tasks` source (`horae_obsidian.source`, #4:
the vault's open tasks), the `obsidian.write_note` sink (`horae_obsidian.sink`, #3: the
core's bytes for `note.*` intents, written into owned files and blocks only) and the
`obsidian.write_task` sink (#4: `task.update_status` edits one task line, `task.create`
appends one to an owned note), the `obsidian.reminders` transform
(`horae_obsidian.reminders`, #9: `notify.push` reminders from the open tasks, no LLM) and the
`obsidian.render_note` transform (`horae_obsidian.render`, #5: the assistant note from a
template, one `note.replace_owned_block`). The
plugin never writes anything outside its sinks: it returns data and proposes intents, and the
core checks the intents against `plugin.yaml`'s permissions, shows them as a dry-run diff and
applies them.
"""

from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin

from horae_obsidian import reminders, render, source
from horae_obsidian.sink import TASK_TOOL, TOOL, VaultSink
from horae_obsidian.tasksink import TASK_TYPES
from horae_obsidian.writer import NOTE_TYPES

NAME = "obsidian"
"""The plugin's name, also the namespace of its refs (`obsidian.open_tasks`) and
fingerprints (`obsidian:id:…`)."""
VERSION = "0.1.1"
COMMAND = ["python3", "-m", "horae_obsidian"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(intents=[*NOTE_TYPES, *TASK_TYPES, "notify.push"])
"""The note and task intents the sinks apply, and `notify.push`, which the
`obsidian.reminders` transform proposes (a notifier plugin applies it). No network or secret,
and no path: where the
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
    def open_tasks(
        call: Call, limit: int = 500, open_statuses: str = source.DEFAULT_OPEN_STATUSES
    ) -> dict[str, Any]:
        return source.open_tasks(call.paths, limit=limit, open_statuses=open_statuses)

    @plugin.transform(reminders.REF)
    def obsidian_reminders(
        call: Call,
        now: Any = None,
        timezone: Any = None,
        window_minutes: Any = None,
        digest_at: Any = None,
        overdue_at: Any = None,
        overdue_for_minutes: Any = None,
        max_overdue: Any = None,
        quiet_hours: Any = None,
    ) -> Output:
        """`notify.push` reminders, the morning digest and overdue nudges from the collected
        `obsidian.open_tasks` data (`horae_obsidian.reminders`). No clock: `now` and
        `timezone` are the pipeline's `{{now}}` and `{{timezone}}`."""
        data, intents = reminders.reminders(call.params, call.input)
        return Output(data, intents)

    @plugin.transform(render.REF)
    def obsidian_render_note(
        call: Call,
        today: Any = None,
        block: Any = None,
        path: Any = None,
        template: Any = None,
        timezone: Any = None,
        summary: Any = None,
        ranked: Any = None,
        expect: Any = None,
        max_items: Any = None,
    ) -> Output:
        """One `note.replace_owned_block` proposal: the assistant note rendered from the
        collected data and a logic-less template (`horae_obsidian.render`). No clock: `today`
        is the pipeline's `{{date}}`."""
        data, intents = render.render_note(call.params, call.input)
        return Output(data, intents)

    plugin.add(VaultSink([TOOL, TASK_TOOL]))
    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_obsidian --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=">=0,<0.2",  # the core is 0.0.x until v0.1; nothing checks it yet
        license="Apache-2.0",
    )
