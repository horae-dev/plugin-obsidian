# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.render_note` transform (#5): the assistant note, rendered without an LLM.

It takes the collected data (the `obsidian.open_tasks` tasks, calendar events, GitHub items,
and optionally the LLM's `summary` / `ranked`) and proposes **one** `note.replace_owned_block`
intent whose content is Markdown made here from a template. Where this sits: the daily
brief's LLM words the summary and the ranking, this transform lays the note out, so the
core's note-text guard (schemas decision 55: no Tasks checkbox, wikilink, `#tag`, `::` in LLM
note text) never has to judge the structure, and the same input always gives the same bytes.

**Deterministic.** No clock, file, environment or network: `today` is a parameter (the
pipeline's `{{date}}`), the order is fixed (sorted by date, time, text, key), and a run and
its replay propose the same bytes.

**Inert.** Every untrusted string (a task description, an event or pull request title, the
LLM's text) goes through the SDK's `escape_title` and is written as a plain bullet, never as a
checkbox: it cannot become a live task, link, tag, field or Tasks signifier, and the note
holds no `[ ]` that Tasks, or a later `obsidian.open_tasks` read, would count as a task.

**Sections** (placeholders of the template, `${name}`): `calendar`, `tasks` (grouped by
due date: Overdue, Today, Tomorrow, Later, No date), `github`, `focus` (the LLM's `ranked`),
`summary`, `alerts` (what the note does not show, see below) and `date`. A section with
nothing in it reads `_None._`. At most `max_items` lines per section or task group, then
`…and N more`.

**Alerts** say what is missing, so a short note is not read as a quiet day: the source left
tasks out (`more`, `output_cut`, `withheld`, `hidden`, `skipped`), entries that could not be
read, and a source listed in `expect` that gave no data (it failed or was not collected).

**The template** is the `template` parameter (per instance: the pipeline's parameters are
the instance's own configuration, trusted, and never read from the vault, whose notes are
not). It is logic-less: literal text, `${name}` placeholders for the sections above and `$$`
for a dollar sign, nothing else (no loops, conditions, expressions or includes; `{{` is the
core's own parameter syntax, so it is not used). Placeholders are replaced in one pass, so
what a section holds is never expanded again. A template is refused (`RenderError`) when it
has an unknown placeholder or a stray `$`, is longer than `MAX_TEMPLATE` characters, or holds
what the core's guard refuses in note text: a checkbox line, wikilink, `#tag`, `::`, `%%`,
a Tasks signifier, an owned-block marker, a code fence, `<%`, a frontmatter block, a control
character.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae_sdk import PluginError
from horae_sdk.intents import IntentError, note_replace_owned_block
from horae_sdk.tasks import SIGNIFIERS, escape_title

from horae_obsidian.reminders import clean
from horae_obsidian.source import DEFAULT_OPEN_STATUSES

REF = "obsidian.render_note"
TASKS_SOURCE = "obsidian.open_tasks"
DEFAULT_BLOCK = "assistant-daily"
MAX_TEMPLATE = 4000
MAX_RENDERED = 64 * 1024
"""Characters of the rendered note (the intent allows 1 MiB; a daily note is read by a human)."""
MAX_LINE = 200
MAX_SUMMARY = 500
DEFAULT_MAX_ITEMS = 20
MAX_ITEMS = 100
MAX_SCANNED = 5000
"""Entries read per source entry: the rest is counted as unread (an alert)."""
NONE_LINE = "_None._"

SECTIONS = ("date", "summary", "focus", "alerts", "calendar", "tasks", "github")
DEFAULT_TEMPLATE = """\
# Assistant, ${date}

${summary}

## Alerts

${alerts}

## Focus

${focus}

## Calendar

${calendar}

## Tasks

${tasks}

## GitHub

${github}
"""

_KNOWN = frozenset(
    {"today", "block", "path", "template", "timezone", "summary", "ranked", "expect", "max_items"}
)
_PLACEHOLDER = re.compile(r"\$(?:\$|\{([a-z_]+)\})")
_CHECKBOX = re.compile(r"^[ \t>]*(?:[-*+]|\d+[.)])[ \t]+\[.\]", re.MULTILINE)
_TAG = re.compile(r"(?<![\w#\\])#[^\s#]")
_MARKER = re.compile(r"horae:(?:begin|end)", re.IGNORECASE)
_UNSAFE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\x85\u2028\u2029\u200b-\u200f\u202a-\u202e\ufeff]")
_BREAKS = re.compile(r"\n{3,}")
_WHY = ("overdue", "today", "later")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_REF = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


class RenderError(PluginError):
    """The parameters, the template or the input are not what the transform expects."""


def check_template(template: object) -> str:
    """`template` if it is a valid template, else `RenderError` naming the reason."""
    if not isinstance(template, str) or not template.strip():
        raise RenderError("template must be non-empty text")
    if len(template) > MAX_TEMPLATE:
        raise RenderError(f"template is longer than {MAX_TEMPLATE} characters")
    if _UNSAFE.search(template) or "\r" in template:
        raise RenderError("template holds a control character (use line feeds)")
    if "$" in _PLACEHOLDER.sub("", template):
        raise RenderError("template: a dollar sign is `$$`, a placeholder is `${name}`")
    for match in _PLACEHOLDER.finditer(template):
        name = match.group(1)
        if name is not None and name not in SECTIONS:
            raise RenderError(f"template: unknown placeholder ${{{name}}}")
    refused = {
        "a checkbox line": _CHECKBOX.search(template),
        "a wikilink": "[[" in template,
        "a `::` field": "::" in template,
        "a `%%` comment": "%%" in template,
        "a `#tag`": _TAG.search(template),
        "an owned-block marker": _MARKER.search(template),
        "a code fence": "```" in template or "~~~" in template,
        "a `<%` template tag": "<%" in template,
        "a frontmatter block": template.startswith("---"),
        "a Tasks signifier": any(sign in template for sign in SIGNIFIERS),
    }
    for what, found in refused.items():
        if found:
            raise RenderError(f"template holds {what}, which a note must not hold")
    return template


def _fill(template: str, values: Mapping[str, str]) -> str:
    """One pass: `$$` is `$`, `${name}` its value; what a value holds is not expanded."""

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        return "$" if name is None else values[name]

    return _PLACEHOLDER.sub(one, template)


def _line(text: object, limit: int = MAX_LINE) -> str:
    """Untrusted `text` as one inert line (the SDK's `escape_title`)."""
    return escape_title(text if isinstance(text, str) else "", max_length=limit)


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


@dataclass
class Settings:
    today: date
    block: str
    path: str | None
    template: str
    zone: ZoneInfo | None
    summary: str
    ranked: list[tuple[str, str]]
    expect: list[str]
    max_items: int


def _ranked(value: object) -> list[tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RenderError("ranked is a list of {title, why}")
    out: list[tuple[str, str]] = []
    for item in value[:MAX_ITEMS]:
        if not isinstance(item, Mapping) or not isinstance(item.get("title"), str):
            raise RenderError("ranked is a list of {title, why}")
        why = item.get("why")
        out.append((item["title"], why if why in _WHY else ""))
    return out


def parse_params(params: Mapping[str, Any]) -> Settings:
    """The transform's parameters; an unknown or invalid one is an error (never ignored). A
    parameter given as `None` is omitted."""
    params = {k: v for k, v in params.items() if v is not None}
    unknown = sorted(str(name) for name in set(params) - _KNOWN)
    if unknown:
        raise RenderError(f"unknown parameters: {', '.join(unknown)}")
    today = _day(params.get("today"))
    if today is None:
        raise RenderError("today must be a YYYY-MM-DD date (`{{date}}`)")
    block = params.get("block", DEFAULT_BLOCK)
    path = params.get("path")
    try:
        note_replace_owned_block(block, "", path=path)
    except IntentError as error:
        raise RenderError(f"block or path: {error}") from None
    name = params.get("timezone")
    zone = None
    if name is not None:
        try:
            zone = ZoneInfo(name) if isinstance(name, str) and name else None
        except (ZoneInfoNotFoundError, ValueError, OSError):
            zone = None
        if zone is None:
            raise RenderError("timezone must be an IANA time zone (`{{timezone}}`)")
    summary = params.get("summary", "")
    if not isinstance(summary, str):
        raise RenderError("summary is text")
    expect = params.get("expect", [])
    if not isinstance(expect, list) or not all(
        isinstance(e, str) and _REF.fullmatch(e) for e in expect
    ):
        raise RenderError("expect is a list of source refs (`calendar.events`)")
    items = params.get("max_items", DEFAULT_MAX_ITEMS)
    if isinstance(items, bool) or not isinstance(items, int) or not 1 <= items <= MAX_ITEMS:
        raise RenderError(f"max_items must be an integer from 1 to {MAX_ITEMS}")
    return Settings(
        today=today,
        block=block,
        path=path,
        template=check_template(params.get("template", DEFAULT_TEMPLATE)),
        zone=zone,
        summary=summary,
        ranked=_ranked(params.get("ranked")),
        expect=sorted(set(expect)),
        max_items=items,
    )


@dataclass
class Collected:
    tasks: list[tuple[date | None, str, str]]
    """`(due, description, key)` of the open tasks."""
    events: list[tuple[str, str, str]]
    """`(day, time, title)` with `day` ISO and `time` `HH:MM`, `""` for an all-day event."""
    items: list[str]
    seen: set[str]
    alerts: list[str]


def _entries(data: object, *names: str) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for name in names:
            found = data.get(name)
            if isinstance(found, list):
                return found
    return None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _listed(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _task(raw: object, statuses: str) -> tuple[date | None, str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    char = raw.get("status_char")
    if (
        not isinstance(char, str)
        or len(char) != 1
        or char not in statuses
        or char in "xX-"
        or raw.get("status") in ("done", "cancelled")
    ):
        return None
    description = raw.get("description")
    link = raw.get("link")
    if isinstance(description, str) and isinstance(link, str):
        description = description.replace(f"[link]({link})", "")  # the link Horae wrote
    due = raw.get("due")
    key = raw.get("key")
    return (_day(due), clean(description, MAX_LINE), key if isinstance(key, str) else "")


def _when(raw: object, zone: ZoneInfo | None) -> tuple[str, str] | None:
    """`(day, HH:MM)` of an event's start, `HH:MM` empty for an all-day one."""
    if isinstance(raw, Mapping):
        raw = raw.get("dateTime") or raw.get("date")
    if not isinstance(raw, str):
        return None
    if _day(raw) is not None:
        return raw, ""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if zone is not None and moment.tzinfo is not None:
        moment = moment.astimezone(zone)
    return moment.date().isoformat(), f"{moment:%H:%M}"


def _collect(input: object, settings: Settings) -> Collected:
    if not isinstance(input, list):
        raise RenderError("the input is the collect step's list of sources")
    out = Collected(tasks=[], events=[], items=[], seen=set(), alerts=[])
    unreadable = 0
    skipped = 0
    for entry in input:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("source"), str):
            continue
        ref: str = entry["source"]
        data = entry.get("data")
        out.seen.add(ref)
        if ref == TASKS_SOURCE:
            raws = _entries(data, "tasks")
            if raws is None or not isinstance(data, Mapping):
                unreadable += 1
                continue
            statuses = data.get("open_statuses", DEFAULT_OPEN_STATUSES)
            statuses = statuses if isinstance(statuses, str) else DEFAULT_OPEN_STATUSES
            for raw in raws[:MAX_SCANNED]:
                task = _task(raw, statuses)
                if task is not None:
                    out.tasks.append(task)
            skipped += max(0, len(raws) - MAX_SCANNED)
            left = _count(data.get("more"))
            if left:
                out.alerts.append(f"{left} open tasks were left out by the task source")
            if data.get("output_cut") is True:
                out.alerts.append("the task source cut its output")
            for what, count in (
                ("withheld (ambiguous key)", _listed(data.get("withheld"))),
                ("hidden (Horae's own output)", _listed(data.get("hidden"))),
                ("notes that could not be read", _listed(data.get("skipped"))),
            ):
                if count:
                    out.alerts.append(f"{count} tasks or lines {what}")
        elif ref.startswith("calendar."):
            raws = _entries(data, "events", "items")
            if raws is None:
                unreadable += 1
                continue
            for raw in raws[:MAX_SCANNED]:
                when = _when(raw.get("start"), settings.zone) if isinstance(raw, Mapping) else None
                title = raw.get("title", raw.get("summary")) if isinstance(raw, Mapping) else None
                if when is None or not isinstance(title, str):
                    skipped += 1
                else:
                    out.events.append((when[0], when[1], title))
            skipped += max(0, len(raws) - MAX_SCANNED)
        elif ref.startswith("github."):
            raws = _entries(data, "items", "pull_requests", "issues")
            if raws is None:
                unreadable += 1
                continue
            for raw in raws[:MAX_SCANNED]:
                title = raw.get("title") if isinstance(raw, Mapping) else None
                if not isinstance(title, str):
                    skipped += 1
                    continue
                repo = raw.get("repo", raw.get("repository"))
                number = raw.get("number")
                prefix = repo if isinstance(repo, str) and _REPO.fullmatch(repo) else ""
                if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                    prefix = f"{prefix} {number}".strip()
                out.items.append(f"{prefix} {_line(title)}".strip())
            skipped += max(0, len(raws) - MAX_SCANNED)
    if unreadable:
        out.alerts.append(f"{unreadable} sources returned data that could not be read")
    if skipped:
        out.alerts.append(f"{skipped} entries could not be read")
    out.alerts += [f"no data from {ref}" for ref in settings.expect if ref not in out.seen]
    return out


def _bullets(lines: list[str], limit: int) -> list[str]:
    shown = [f"- {line}" for line in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"- …and {len(lines) - limit} more")
    return shown


def _block(lines: list[str]) -> str:
    return "\n".join(lines) if lines else NONE_LINE


def _tasks_section(tasks: list[tuple[date | None, str, str]], s: Settings) -> str:
    tomorrow = s.today + timedelta(days=1)
    groups: dict[str, list[tuple[date | None, str, str]]] = {
        "Overdue": [],
        "Today": [],
        "Tomorrow": [],
        "Later": [],
        "No date": [],
    }
    for task in tasks:
        due = task[0]
        if due is None:
            groups["No date"].append(task)
        elif due < s.today:
            groups["Overdue"].append(task)
        elif due == s.today:
            groups["Today"].append(task)
        elif due == tomorrow:
            groups["Tomorrow"].append(task)
        else:
            groups["Later"].append(task)
    parts: list[str] = []
    for name, group in groups.items():
        if not group:
            continue
        group.sort(key=lambda t: (t[0] or date.max, t[1], t[2]))
        lines = [
            _line(t[1])
            + (f" (due {t[0].isoformat()})" if name in ("Overdue", "Later") and t[0] else "")
            for t in group
        ]
        parts.append(f"### {name} ({len(group)})\n\n" + "\n".join(_bullets(lines, s.max_items)))
    return "\n\n".join(parts) if parts else NONE_LINE


def _calendar_section(events: list[tuple[str, str, str]], s: Settings) -> str:
    lines = []
    for day, at, title in sorted(set(events)):
        stamp = (at or "all day") if day == s.today.isoformat() else f"{day} {at}".strip()
        lines.append(f"{stamp} {_line(title)}")
    return _block(_bullets(lines, s.max_items))


def _focus_section(s: Settings) -> str:
    lines = [_line(f"{title} ({why})" if why else title) for title, why in s.ranked]
    return _block(_bullets(lines, s.max_items))


def render(settings: Settings, input: object) -> tuple[str, Collected]:
    """`(Markdown, collected)`: the note for `settings` from the collected `input`."""
    found = _collect(input, settings)
    values = {
        "date": settings.today.isoformat(),
        "summary": escape_title(settings.summary, max_length=MAX_SUMMARY)
        if settings.summary.strip()
        else "",
        "focus": _focus_section(settings),
        "alerts": _block(_bullets(found.alerts, settings.max_items)),
        "calendar": _calendar_section(found.events, settings),
        "tasks": _tasks_section(found.tasks, settings),
        "github": _block(_bullets(sorted(found.items), settings.max_items)),
    }
    text = _BREAKS.sub("\n\n", _fill(settings.template, values)).strip("\n") + "\n"
    if len(text) > MAX_RENDERED:
        raise RenderError(f"the note is longer than {MAX_RENDERED} characters")
    return text, found


def render_note(
    params: Mapping[str, Any], input: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(data, intents)`: a summary of what was rendered and the one
    `note.replace_owned_block` proposal."""
    settings = parse_params(params)
    text, found = render(settings, input)
    data = {
        "block": settings.block,
        "today": settings.today.isoformat(),
        "characters": len(text),
        "tasks": len(found.tasks),
        "events": len(found.events),
        "items": len(found.items),
        "alerts": len(found.alerts),
    }
    return data, [note_replace_owned_block(settings.block, text, path=settings.path)]


__all__ = [
    "DEFAULT_TEMPLATE",
    "MAX_RENDERED",
    "MAX_TEMPLATE",
    "REF",
    "RenderError",
    "check_template",
    "parse_params",
    "render",
    "render_note",
]
