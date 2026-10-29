# SPDX-License-Identifier: Apache-2.0
"""The plugin: one source, `obsidian.inbox`, that proposes a task per message.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_obsidian`). The plugin never writes anything: it returns data and proposes intents,
which the core checks against `plugin.yaml`'s permissions, shows as a dry-run diff and
applies.

A message comes from a service the plugin does not control, so its fields are **untrusted**:
the subject reaches the task title only through `intents.task_create`, which escapes it for
Obsidian Tasks (a subject holding `📅 2020-01-01` must not schedule the task).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from horae_sdk import Call, Output, Permissions, Plugin, intents

NAME = "obsidian"
"""The plugin's name, also the namespace of its refs (`obsidian.inbox`) and fingerprints."""
VERSION = "0.1.0"
COMMAND = ["python3", "-m", "horae_obsidian"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(intents=["task.create"])
"""Declare the least you need: every network host, secret, path and intent type."""


class Inbox(Protocol):
    """Where the messages come from. Replace it with your service's client; the tests give the
    plugin a fake one that answers from synthetic fixtures."""

    def messages(self, folder: str) -> Sequence[Mapping[str, Any]]:
        """The messages of `folder`, each a mapping with at least `id` and `subject`."""
        ...


class SampleInbox:
    """A stand-in client with one synthetic message, so the plugin runs as shipped."""

    def messages(self, folder: str) -> Sequence[Mapping[str, Any]]:
        return [{"id": "welcome", "subject": f"Welcome to {folder}"}]


def build(inbox: Inbox) -> Plugin:
    """The plugin, reading its messages from `inbox`."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Proposes a task to reply to each message of a folder.",
    )

    @plugin.source(f"{NAME}.inbox")
    def inbox_source(call: Call, folder: str = "inbox") -> Output:
        # `folder` is a pipeline parameter (trusted); every message field is untrusted data.
        seen: list[dict[str, str]] = []
        tasks: list[dict[str, Any]] = []
        for message in inbox.messages(folder):
            message_id, subject = message.get("id"), message.get("subject")
            if not isinstance(message_id, str) or not isinstance(subject, str):
                continue
            seen.append({"id": message_id, "subject": subject})
            key = intents.fingerprint(NAME, "reply", message_id)
            tasks.append(intents.task_create(f"Reply to {subject}", key=key))
        return Output({"folder": folder, "messages": seen}, intents=tasks)

    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_obsidian --manifest`)."""
    return build(SampleInbox()).manifest(command=COMMAND, license="Apache-2.0")
