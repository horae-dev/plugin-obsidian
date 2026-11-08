# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.reminders` transform (#9): `obsidian.open_tasks` data to `notify.push`
intents, with no LLM, for pack-reminders' `remind` pipeline.

Ported from pack-reminders' reference implementation (horae-dev/pack-reminders#4) with the
maintainer's review decisions. It reads no clock, file or environment: everything comes from
its parameters (the pipeline passes `{{now}}` and `{{timezone}}`) and its input, so a run and
its replay propose the same pushes. Every time below is a wall-clock time in `timezone`, read
with the zone's rules (a skipped time is the first instant after the gap, a repeated one its
first occurrence: Debian cron's rules, as the core's scheduler, schemas decision 49 B).

- **Open tasks only.** A task counts when its status character is one of the source's
  `open_statuses` (default `" /"`) and it is neither `done` nor `cancelled` (`x`, `X`, `-`).
- **The lookback** is `(end - window_minutes, end]`, `end` being `now` rounded down to the
  minute; default 60 minutes. Every run proposes what falls in its lookback, so a run that
  misfired is caught up by the next one, and the notifier's daily dedupe (the text is stable)
  sends each push once. `window_minutes` must be at least the cron interval (the operator's
  choice; a shorter lookback leaves gaps between runs).
- **`⏰` reminders**: `⏰ HH:MM` is that time on the due date (no due date: no reminder),
  `⏰ YYYY-MM-DD HH:MM` that date and time. One push when the instant is in the lookback,
  titled with the date and time (`⏰ 2026-11-09 08:00`), so a daily recurring reminder is a
  new push each day.
- **Snoozed** is recomputed here from the `⏰` and `now` (the source's flag is only a hint):
  a `⏰` date-time after `now`, or a date-only `⏰` after today. A snoozed task gets no nudge;
  it stays in the digest when it is due that day or reminds that day (by reminder time).
- **The digest**: proposed by every run whose lookback holds `digest_at` (on today or the day
  before, for a configuration that crosses midnight): the overdue tasks (a count and the
  oldest `max_overdue`), then the day's tasks by reminder time, then the rest due that day.
  A task whose reminder fell in `quiet_hours` before the digest (since the previous one) is
  left out of those sections when `quiet_hours.during` is `delay` (the default): the notifier
  delays its own push to the end of the quiet hours. With `during: drop` the notifier would
  drop that push, so no individual push is proposed for a quiet-hours reminder and the digest
  lists it instead, under `During quiet hours:` (`- ⏰ 03:00 …`). At most `DIGEST_BYTES` of
  UTF-8, never cut mid-line; `UNREAD_LINE` when the source left tasks out (`more`, `output_cut`,
  `withheld`, `hidden`, `skipped`). None when nothing is listed.
- **Overdue nudges**: one push per task that became overdue (due the day before), from
  `overdue_at` for `overdue_for_minutes`, at most `max_overdue`. Older overdue tasks are only
  in the digest.
- **Stable text**: titles and bodies hold the task's description, dates and times, never a
  countdown or the run's time. Descriptions are untrusted: Markdown links become their text,
  `#tags` are dropped, control characters and line breaks become spaces, and they are cut to
  `MAX_DESCRIPTION` characters on a word boundary. When two pushes of one run would have the
  same text, each body gets a short suffix from its task key (` · 3fa2`).
"""

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae_sdk import PluginError
from horae_sdk.intents import notify_push

REF = "obsidian.reminders"
SOURCE = "obsidian.open_tasks"

DEFAULT_OPEN_STATUSES = " /"
CLOSED_STATUSES = frozenset({"done", "cancelled"})
CLOSED_CHARS = frozenset("xX-")

DEFAULT_WINDOW_MINUTES = 60
MAX_WINDOW_MINUTES = 12 * 60
DEFAULT_OVERDUE_FOR_MINUTES = 60
DEFAULT_MAX_OVERDUE = 10
MAX_OVERDUE = 100
MAX_DESCRIPTION = 200
"""Characters of a description in a push; a longer one is cut on a word boundary, with `…`."""
MAX_TITLE = 200
DIGEST_BYTES = 3800
"""UTF-8 bytes of a digest body (under the intent's 4096 characters, and ntfy's 4 KiB)."""
UNREAD_LINE = "Some tasks could not be read"

_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]")
_SPACES = re.compile(r"\s+")
_MD_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_WIKI_LINK = re.compile(r"!?\[\[([^\]|\n]*)(?:\|([^\]\n]*))?\]\]")
_TAG = re.compile(r"(?<!\S)#([^\s#]+)")
_KNOWN = frozenset(
    {
        "now",
        "timezone",
        "window_minutes",
        "digest_at",
        "overdue_at",
        "overdue_for_minutes",
        "max_overdue",
        "quiet_hours",
    }
)


class ReminderError(PluginError):
    """The parameters or the input are not what the transform expects (the call fails)."""


@dataclass(frozen=True)
class Params:
    now: datetime
    zone: ZoneInfo
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    digest_at: time | None = None
    overdue_at: time | None = None
    overdue_for_minutes: int = DEFAULT_OVERDUE_FOR_MINUTES
    max_overdue: int = DEFAULT_MAX_OVERDUE
    quiet_hours: tuple[time, time] | None = None
    quiet_drops: bool = False
    """`quiet_hours.during` is `drop`: the notifier drops pushes proposed in quiet hours."""


def _int(params: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = params.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ReminderError(f"{name} must be an integer from {low} to {high}")
    return value


def _clock(value: object, what: str) -> time:
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ReminderError(f'{what} must be a time "HH:MM"')
    return time(int(match.group(1)), int(match.group(2)))


def _optional_clock(params: Mapping[str, Any], name: str) -> time | None:
    value = params.get(name)
    return None if value is None else _clock(value, name)


def _quiet_hours(value: object) -> tuple[time, time, bool] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not set(value) <= {"start", "end", "during"}:
        raise ReminderError('quiet_hours is {start: "HH:MM", end: "HH:MM", during: delay|drop}')
    during = value.get("during", "delay")
    if during not in ("delay", "drop"):
        raise ReminderError("quiet_hours.during must be delay or drop")
    start = _clock(value.get("start"), "quiet_hours.start")
    end = _clock(value.get("end"), "quiet_hours.end")
    if start == end:
        raise ReminderError("quiet_hours.start and quiet_hours.end must differ")
    return start, end, during == "drop"


def parse_params(params: Mapping[str, Any]) -> Params:
    """The transform's parameters; an unknown or invalid one is an error (never ignored). A
    parameter given as `None` is omitted."""
    unknown = sorted(str(name) for name in set(params) - _KNOWN)
    if unknown:
        raise ReminderError(f"unknown parameters: {', '.join(unknown)}")
    raw_now = params.get("now")
    try:
        now = datetime.fromisoformat(raw_now) if isinstance(raw_now, str) else None
    except ValueError:
        now = None
    if now is None or now.tzinfo is None:
        raise ReminderError("now must be an RFC 3339 time with an offset (`{{now}}`)")
    name = params.get("timezone")
    try:
        zone = ZoneInfo(name) if isinstance(name, str) and name else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = None
    if zone is None:
        raise ReminderError("timezone must be an IANA time zone (`{{timezone}}`)")
    quiet = _quiet_hours(params.get("quiet_hours"))
    return Params(
        now=now.astimezone(UTC),
        zone=zone,
        window_minutes=_int(
            params, "window_minutes", DEFAULT_WINDOW_MINUTES, 1, MAX_WINDOW_MINUTES
        ),
        digest_at=_optional_clock(params, "digest_at"),
        overdue_at=_optional_clock(params, "overdue_at"),
        overdue_for_minutes=_int(
            params, "overdue_for_minutes", DEFAULT_OVERDUE_FOR_MINUTES, 1, 24 * 60
        ),
        max_overdue=_int(params, "max_overdue", DEFAULT_MAX_OVERDUE, 0, MAX_OVERDUE),
        quiet_hours=quiet[:2] if quiet else None,
        quiet_drops=bool(quiet and quiet[2]),
    )


def instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The UTC instant of wall-clock `at` on `day` in `zone`: the first occurrence of a
    repeated time, the first instant after the gap for a skipped one."""
    naive = datetime.combine(day, at)
    moment = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if moment.astimezone(zone).replace(tzinfo=None) == naive:
        return moment
    # In a gap: `fold=0` read it with the offset before the transition, which lands after the
    # gap. Walk back to the transition (the first instant whose wall clock is past `naive`).
    step = timedelta(minutes=1)
    for _ in range(48 * 60):
        if (moment - step).astimezone(zone).replace(tzinfo=None) < naive:
            return moment
        moment -= step
    return moment  # pragma: no cover - no zone has a gap of two days


def _day(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReminderError("a date is a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ReminderError("a date is a YYYY-MM-DD string") from None


def _untag(match: re.Match[str]) -> str:
    return match.group(0) if match.group(1).isdigit() else ""


def clean(text: object, limit: int = MAX_DESCRIPTION) -> str:
    """Untrusted Markdown as one line of plain text: links become their text, `#tags` are
    dropped, control characters and runs of spaces become one space, cut to `limit`
    characters on a word boundary with `…`. Never empty."""
    line = text if isinstance(text, str) else ""
    line = _MD_LINK.sub(lambda m: m.group(1), line)
    line = _WIKI_LINK.sub(lambda m: m.group(2) or m.group(1), line)
    line = _UNSAFE.sub(" ", line)
    line = _TAG.sub(_untag, line)
    line = _SPACES.sub(" ", line).strip()
    if len(line) > limit:
        cut = line[: limit - 1]
        space = cut.rfind(" ")
        if space >= limit // 2:
            cut = cut[:space]
        line = cut.rstrip() + "…"
    return line or "(no description)"


@dataclass(frozen=True)
class Task:
    """What the transform reads of one `open_tasks` entry."""

    key: str
    description: str
    due: date | None
    reminder_day: date | None
    """The `⏰` date as written (`None` for a bare `⏰ HH:MM`)."""
    reminder_time: time | None
    reminder_at: datetime | None
    """The `⏰` instant: the time on the reminder date, else on the due date."""
    snoozed: bool

    def reminds_on(self, day: date, zone: ZoneInfo) -> bool:
        if self.reminder_at is not None:
            return self.reminder_at.astimezone(zone).date() == day
        return self.reminder_time is None and self.reminder_day == day


def _is_open(raw: Mapping[str, Any], open_statuses: str) -> bool:
    char = raw.get("status_char")
    return (
        isinstance(char, str)
        and len(char) == 1
        and char in open_statuses
        and char not in CLOSED_CHARS
        and raw.get("status") not in CLOSED_STATUSES
    )


def _description(raw: Mapping[str, Any]) -> str:
    text = raw.get("description")
    link = raw.get("link")
    if isinstance(text, str) and isinstance(link, str):
        text = text.replace(f"[link]({link})", "")  # the link Horae wrote, not the user's
    return clean(text)


def _task(raw: Mapping[str, Any], p: Params, today: date) -> Task:
    reminder = raw.get("reminder")
    if reminder is not None and not isinstance(reminder, Mapping):
        raise ReminderError("a reminder is an object")
    reminder = reminder or {}
    reminder_day = _day(reminder.get("date"))
    raw_time = reminder.get("time")
    reminder_time = None if raw_time is None else _clock(raw_time, "a reminder time")
    due = _day(raw.get("due"))
    day = reminder_day or due
    at = (
        instant(day, reminder_time, p.zone)
        if reminder_time is not None and day is not None
        else None
    )
    if reminder_day is None:
        snoozed = False  # a bare `⏰ HH:MM` is a reminder on the due date, not a snooze
    elif at is not None:
        snoozed = at > p.now
    else:
        snoozed = reminder_day > today
    key = raw.get("key")
    return Task(
        key=key if isinstance(key, str) else "",
        description=_description(raw),
        due=due,
        reminder_day=reminder_day,
        reminder_time=reminder_time,
        reminder_at=at,
        snoozed=snoozed,
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _listed(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


@dataclass
class Collected:
    tasks: list[Task]
    invalid: int = 0
    more: int = 0
    output_cut: bool = False
    withheld: int = 0
    hidden: int = 0
    skipped: int = 0

    @property
    def unread(self) -> bool:
        return bool(self.more or self.output_cut or self.withheld or self.hidden or self.skipped)


def _collect(input: Any, p: Params, today: date) -> Collected:
    """The open tasks of every `open_tasks` source in `input`, the malformed ones skipped
    (counted), and what the sources left out."""
    if not isinstance(input, list):
        raise ReminderError("the input is the collect step's list of sources")
    sources = [e for e in input if isinstance(e, Mapping) and e.get("source") == SOURCE]
    if not sources:
        raise ReminderError(f"the input has no {SOURCE} data")
    out = Collected(tasks=[])
    for source in sources:
        data = source.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("tasks"), list):
            raise ReminderError(f"{SOURCE} data has no task list")
        statuses = data.get("open_statuses", DEFAULT_OPEN_STATUSES)
        if not isinstance(statuses, str):
            raise ReminderError("open_statuses is a string")
        out.more += _count(data.get("more"))
        out.output_cut = out.output_cut or data.get("output_cut") is True
        out.withheld += _listed(data.get("withheld")) + _count(data.get("withheld_more"))
        out.hidden += _listed(data.get("hidden")) + _count(data.get("hidden_more"))
        out.skipped += _listed(data.get("skipped"))
        for raw in data["tasks"]:
            if not isinstance(raw, Mapping) or not _is_open(raw, statuses):
                continue
            try:
                out.tasks.append(_task(raw, p, today))
            except ReminderError:
                out.invalid += 1
    return out


def _quiet(moment: datetime, zone: ZoneInfo, hours: tuple[time, time]) -> bool:
    at = moment.astimezone(zone).time()
    start, end = hours
    return start <= at < end if start < end else at >= start or at < end


def _utf8(line: str) -> int:
    return len(line.encode("utf-8")) + 1  # and its line break


def _digest(
    tasks: list[Task], day: date, at: datetime, p: Params, unread: bool
) -> dict[str, Any] | None:
    """The digest of `day` (proposed at `at`), or `None` when it lists nothing."""

    def in_quiet(task: Task) -> bool:
        """Its reminder fell in quiet hours since the previous digest."""
        r = task.reminder_at
        return (
            p.quiet_hours is not None
            and r is not None
            and at - timedelta(days=1) < r <= at
            and _quiet(r, p.zone, p.quiet_hours)
        )

    overdue: list[Task] = []
    timed: list[Task] = []
    rest: list[Task] = []
    night: list[Task] = []
    for task in tasks:
        if in_quiet(task):
            # `delay`: its own push arrives when the quiet hours end. `drop`: it has none.
            if p.quiet_drops:
                night.append(task)
            continue
        reminds = task.reminds_on(day, p.zone)
        due_today = task.due == day
        if task.snoozed and not (reminds or due_today):
            continue
        if reminds and task.reminder_at is not None:
            timed.append(task)
        elif task.due is not None and task.due < day:
            overdue.append(task)
        elif due_today or reminds:
            rest.append(task)
    if not (overdue or night or timed or rest):
        return None
    overdue.sort(key=lambda task: task.due or date.max)
    night.sort(key=lambda task: task.reminder_at or datetime.max.replace(tzinfo=UTC))
    timed.sort(key=lambda task: task.reminder_at or datetime.max.replace(tzinfo=UTC))

    footer = [UNREAD_LINE] if unread else []
    budget = DIGEST_BYTES - sum(_utf8(line) for line in footer)
    budget -= _utf8("…and 99999 more")  # room for the line that says what did not fit
    lines: list[str] = []
    left = 0

    def add(line: str) -> bool:
        nonlocal budget
        if _utf8(line) > budget:
            return False
        lines.append(line)
        budget -= _utf8(line)
        return True

    sections: list[tuple[str, list[str], int]] = []
    if overdue:
        shown = overdue[: p.max_overdue]
        items = [f"- {t.description} (due {t.due.isoformat()})" for t in shown if t.due]
        sections.append((f"Overdue: {len(overdue)}", items, len(overdue) - len(shown)))
    if night:
        items = [f"- ⏰ {t.reminder_time:%H:%M} {t.description}" for t in night if t.reminder_time]
        sections.append((f"During quiet hours: {len(items)}", items, 0))
    if timed or rest:
        items = [
            f"- {t.reminder_time:%H:%M} {t.description}"
            if t.reminder_time is not None
            else f"- {t.description}"
            for t in timed
        ]
        items += [f"- {t.description}" for t in rest]
        sections.append((f"Today: {len(timed) + len(rest)}", items, 0))
    for header, items, cut in sections:
        if left or not add(header):
            left += len(items) + cut
            continue
        for index, item in enumerate(items):
            if not add(item):
                left += len(items) - index
                break
        if cut and (left or not add(f"- …and {cut} more")):
            left += cut
    if left:
        lines.append(f"…and {left} more")
    lines += footer
    return notify_push(
        "\n".join(lines), title=f"Today, {day.isoformat()}", key=f"digest:{day.isoformat()}"
    )


def _digest_of(key: str, length: int) -> str:
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:length]


def _suffix(key: str) -> str:
    return " · " + _digest_of(key, 4)


def _distinct(pushes: list[tuple[str, str, str, str]]) -> list[dict[str, Any]]:
    """`(title, body, task key, push key)` as intents; a body whose title and text another
    push of the set shares gets its task key's stable suffix. The push key is the core's
    stable `key` (decision 57): the same reminder proposed by many runs is sent once."""
    seen = Counter((title, body) for title, body, _, _ in pushes)
    return [
        notify_push(
            body + _suffix(key) if seen[(title, body)] > 1 else body, title=title, key=push_key
        )
        for title, body, key, push_key in pushes
    ]


def _latest_day(
    today: date, at: time | None, zone: ZoneInfo, hit: Callable[[datetime], bool]
) -> date | None:
    """The latest of today and yesterday whose instant of `at` satisfies `hit`."""
    if at is None:
        return None
    for day in (today, today - timedelta(days=1)):
        if hit(instant(day, at, zone)):
            return day
    return None


def reminders(params: Mapping[str, Any], input: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(data, intents)`: a summary of what was decided, and the `notify.push` proposals in
    a fixed order (reminders by time, the digest, overdue nudges)."""
    p = parse_params(params)
    end = p.now.replace(second=0, microsecond=0)
    start = end - timedelta(minutes=p.window_minutes)
    today = end.astimezone(p.zone).date()
    collected = _collect(input, p, today)
    tasks = collected.tasks

    def in_window(moment: datetime) -> bool:
        return start < moment <= end

    due_now = sorted(
        (
            t
            for t in tasks
            if t.reminder_at is not None
            and in_window(t.reminder_at)
            # `during: drop`: a push proposed in quiet hours is dropped; the digest lists it.
            and not (
                p.quiet_drops and p.quiet_hours and _quiet(t.reminder_at, p.zone, p.quiet_hours)
            )
        ),
        key=lambda t: t.reminder_at or end,
    )
    # The date and time as written (a skipped 02:30 still reads 02:30, pushed at 03:00).
    singles = [
        (
            f"⏰ {(t.reminder_day or t.due or today).isoformat()} {t.reminder_time:%H:%M}",
            t.description,
            t.key,
            f"rem:{_digest_of(t.key, 12)}:{(t.reminder_day or t.due or today).isoformat()}"
            f"T{t.reminder_time:%H:%M}",
        )
        for t in due_now
    ]

    digest = None
    digest_day = _latest_day(today, p.digest_at, p.zone, in_window)
    if digest_day is not None and p.digest_at is not None:
        at = instant(digest_day, p.digest_at, p.zone)
        digest = _digest(tasks, digest_day, at, p, collected.unread)

    overdue = [t for t in tasks if not t.snoozed and t.due is not None and t.due < today]
    window = timedelta(minutes=p.overdue_for_minutes)
    nudge_day = _latest_day(
        today, p.overdue_at, p.zone, lambda opens: opens <= end < opens + window
    )
    nudged: list[Task] = []
    if nudge_day is not None:
        yesterday = nudge_day - timedelta(days=1)
        nudged = [t for t in tasks if not t.snoozed and t.due == yesterday][: p.max_overdue]
    nudges = [
        (
            f"Overdue: due {t.due.isoformat()}",
            t.description,
            t.key,
            f"od:{_digest_of(t.key, 12)}:{t.due.isoformat()}:{nudge_day.isoformat()}",
        )
        for t in nudged
        if t.due and nudge_day
    ]

    intents = _distinct(singles)
    if digest is not None:
        intents.append(digest)
    intents += _distinct(nudges)
    data = {
        "window": {"start": _rfc3339(start), "end": _rfc3339(end)},
        "today": today.isoformat(),
        "open_tasks": len(tasks),
        "invalid_tasks": collected.invalid,
        "reminders": len(due_now),
        "digest": digest is not None,
        "overdue": len(overdue),
        "overdue_nudges": len(nudged),
        "pushes": len(intents),
        "more": collected.more,
        "output_cut": collected.output_cut,
        "withheld": collected.withheld,
        "hidden": collected.hidden,
        "skipped": collected.skipped,
    }
    return data, intents


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


__all__ = [
    "DIGEST_BYTES",
    "MAX_DESCRIPTION",
    "REF",
    "SOURCE",
    "UNREAD_LINE",
    "ReminderError",
    "clean",
    "instant",
    "parse_params",
    "reminders",
]
