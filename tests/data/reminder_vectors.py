# SPDX-License-Identifier: Apache-2.0
"""Generate `src/horae_obsidian/vectors/reminders.json`, the golden vectors of the
`obsidian.reminders` transform (pack-reminders reuses them):

    uv run python tests/data/reminder_vectors.py > src/horae_obsidian/vectors/reminders.json

The expected intents are what the transform answers today: review the diff of the JSON file
(tests/test_reminders.py checks the behaviour itself, case by case).
"""

import json
import sys
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from horae_obsidian.reminders import SOURCE, ReminderError, reminders

ZONE = "Europe/Paris"
TODAY = "2026-11-09"
QUIET = {"start": "22:00", "end": "07:00"}


def task(description: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "key": f"obsidian:line:Tasks.md:{description}",
        "description": description,
        "status": "todo",
        "status_char": " ",
        "due": None,
        "reminder": None,
        "snoozed": False,
    }
    entry.update(fields)
    return entry


def at(local: str) -> str:
    moment = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(ZONE)).astimezone(UTC)
    return moment.isoformat().replace("+00:00", "Z")


def params(local: str, **extra: Any) -> dict[str, Any]:
    return {
        "now": at(local),
        "timezone": ZONE,
        "window_minutes": 60,
        "digest_at": "07:00",
        "overdue_at": "09:00",
        "overdue_for_minutes": 60,
        "max_overdue": 10,
        "quiet_hours": {"start": "22:00", "end": "07:00"},
        **extra,
    }


def source(*tasks: dict[str, Any], **data: Any) -> list[dict[str, Any]]:
    return [{"source": SOURCE, "params": {}, "data": {"tasks": list(tasks), **data}}]


def remind(time: str, day: str = TODAY) -> dict[str, Any]:
    return {"due": day, "reminder": {"date": None, "time": time}}


DAY = [
    task("Call the plumber", **remind("14:30")),
    task("Send the insurance form", due="2026-11-05"),
    task("Renew the passport", due="2026-11-08"),
    task("Book the train tickets", due=TODAY),
    task("Read the onboarding guide", due="2026-11-20"),
    task("Take the bins out", **remind("06:30")),
    task("Snoozed to tomorrow", due="2026-11-01", reminder={"date": "2026-11-10", "time": None}),
    task("Snoozed to 16:00", due="2026-11-01", reminder={"date": TODAY, "time": "16:00"}),
    task("Ticked", status="done", status_char="x", **remind("14:30")),
    task("Cancelled", status="cancelled", status_char="-", due="2026-11-08"),
]

CASES: list[tuple[str, dict[str, Any], Any]] = [
    ("reminder at its time", params(f"{TODAY} 14:30"), source(*DAY)),
    ("reminder caught up within the lookback", params(f"{TODAY} 15:25"), source(*DAY)),
    ("reminder past the lookback", params(f"{TODAY} 15:31"), source(*DAY)),
    ("reminder before its time", params(f"{TODAY} 14:29"), source(*DAY)),
    ("date-time reminder ends a snooze", params(f"{TODAY} 16:00"), source(*DAY)),
    ("digest at 07:00", params(f"{TODAY} 07:00"), source(*DAY)),
    ("digest proposed again within the lookback", params(f"{TODAY} 07:55"), source(*DAY)),
    ("no digest past the lookback", params(f"{TODAY} 08:01"), source(*DAY)),
    ("digest of a snoozed day", params("2026-11-10 07:00"), source(*DAY)),
    ("digest says tasks were not read", params(f"{TODAY} 07:00"), source(*DAY, more=3)),
    (
        "digest withheld tasks",
        params(f"{TODAY} 07:00"),
        source(*DAY, withheld=[{"key": "k", "path": "a.md", "line": 2, "reason": "duplicate_key"}]),
    ),
    ("no digest on an empty day", params(f"{TODAY} 07:00"), source(DAY[4])),
    (
        "digest across midnight",
        params("2026-11-10 00:10", digest_at="23:30"),
        source(task("Book the train tickets", due=TODAY)),
    ),
    ("nudge the task that became overdue", params(f"{TODAY} 09:00"), source(*DAY)),
    ("nudge again within its hour", params(f"{TODAY} 09:59"), source(*DAY)),
    ("no nudge after its hour", params(f"{TODAY} 10:00"), source(*DAY)),
    ("nudges the next day", params("2026-11-10 09:00"), source(*DAY)),
    (
        "identical texts get a key suffix",
        params(f"{TODAY} 14:30"),
        source(
            task("Water the plants", **remind("14:30")),
            {**task("Water the plants", **remind("14:30")), "key": "obsidian:line:B.md:3"},
        ),
    ),
    (
        "a daily recurring reminder",
        params("2026-11-10 08:00"),
        source(task("Stretch", recurring=True, **remind("08:00", "2026-11-10"))),
    ),
    (
        "spring forward: a skipped time fires after the gap",
        params("2026-03-29 03:00", window_minutes=5),
        source(task("Check the boiler", **remind("02:30", "2026-03-29"))),
    ),
    (
        "fall back: a repeated time fires at its first occurrence",
        {**params("2026-10-25 02:30", window_minutes=5), "now": "2026-10-25T00:30:00Z"},
        source(task("Check the boiler", **remind("02:30", "2026-10-25"))),
    ),
    (
        "fall back: not again at the second occurrence",
        {**params("2026-10-25 02:30", window_minutes=5), "now": "2026-10-25T01:30:00Z"},
        source(task("Check the boiler", **remind("02:30", "2026-10-25"))),
    ),
    (
        "markdown links and tags are stripped",
        params(f"{TODAY} 14:30"),
        source(
            task(
                "Read [the doc](https://x.test/doc) for [[Project|the project]] #work",
                **remind("14:30"),
            )
        ),
    ),
    (
        "a custom open status",
        params(f"{TODAY} 14:30"),
        source(
            task("Forwarded", status="custom", status_char=">", **remind("14:30")),
            open_statuses=" />",
        ),
    ),
    (
        "quiet hours delay: a night reminder keeps its own push",
        params(f"{TODAY} 03:00", quiet_hours={**QUIET, "during": "delay"}),
        source(task("Night call", **remind("03:00"))),
    ),
    (
        "quiet hours drop: a night reminder has no push of its own",
        params(f"{TODAY} 03:00", quiet_hours={**QUIET, "during": "drop"}),
        source(task("Night call", **remind("03:00"))),
    ),
    (
        "quiet hours drop: the digest lists the night reminders",
        params(f"{TODAY} 07:00", quiet_hours={**QUIET, "during": "drop"}),
        source(task("Night call", **remind("03:00")), task("Plan", due=TODAY)),
    ),
    (
        "quiet hours delay: the digest leaves the night reminders out",
        params(f"{TODAY} 07:00", quiet_hours={**QUIET, "during": "delay"}),
        source(task("Night call", **remind("03:00")), task("Plan", due=TODAY)),
    ),
    (
        "error: bad quiet_hours.during",
        params(f"{TODAY} 03:00", quiet_hours={**QUIET, "during": "x"}),
        source(),
    ),
    ("error: unknown parameter", {**params(f"{TODAY} 14:30"), "window_minute": 5}, source()),
    (
        "error: now without offset",
        {**params(f"{TODAY} 14:30"), "now": "2026-11-09T14:30"},
        source(),
    ),
    ("error: no open_tasks data", params(f"{TODAY} 14:30"), []),
]

DATA_KEYS = ("reminders", "digest", "overdue", "overdue_nudges", "invalid_tasks", "pushes")


def main() -> None:
    vectors = []
    for name, p, data in CASES:
        vector: dict[str, Any] = {"name": name, "params": p, "input": data}
        try:
            summary, intents = reminders(p, data)
        except ReminderError as error:
            vector["error"] = str(error)
        else:
            vector["intents"] = intents
            vector["data"] = {key: summary[key] for key in DATA_KEYS}
        vectors.append(vector)
    json.dump(vectors, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
