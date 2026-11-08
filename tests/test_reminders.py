# SPDX-License-Identifier: Apache-2.0
"""#9: the `obsidian.reminders` transform (ported from pack-reminders' reference, with the
review decisions), on the function directly and through the plugin as the core calls it."""

import json
import re
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import pytest
from horae_sdk.testing import call

from horae_obsidian import build
from horae_obsidian.reminders import (
    DIGEST_BYTES,
    MAX_DESCRIPTION,
    SOURCE,
    UNREAD_LINE,
    ReminderError,
    clean,
    instant,
    parse_params,
    reminders,
)
from horae_obsidian.vectors import reminder_vectors

PARIS = "Europe/Paris"
ZONE = ZoneInfo(PARIS)
TODAY = "2026-11-09"
YESTERDAY = "2026-11-08"
QUIET = {"start": "22:00", "end": "07:00"}


def task(description: str, **fields: Any) -> dict[str, Any]:
    """An `open_tasks` entry in this plugin's shape (the fields the transform reads, and a
    few it does not). The key is derived from the description: stable across runs."""
    entry: dict[str, Any] = {
        "key": f"obsidian:line:Tasks.md:{description}",
        "source_fingerprint": None,
        "description": description,
        "status": "todo",
        "status_char": " ",
        "path": "Tasks.md",
        "line": 1,
        "due": None,
        "reminder": None,
        "snoozed_until": None,
        "snoozed": False,
        "recurring": False,
    }
    entry.update(fields)
    return entry


def collected(*tasks: dict[str, Any], **data: Any) -> list[dict[str, Any]]:
    return [
        {
            "source": SOURCE,
            "params": {},
            "data": {"tasks": list(tasks), "more": 0, "open_statuses": " /", **data},
        }
    ]


def params(at: str, day: str = TODAY, **extra: Any) -> dict[str, Any]:
    """The pipeline's parameters, rendered for a run at Paris time `at` on `day`."""
    now = datetime.fromisoformat(f"{day} {at}").replace(tzinfo=ZONE).astimezone(UTC)
    base: dict[str, Any] = {
        "now": now.isoformat().replace("+00:00", "Z"),
        "timezone": PARIS,
        "window_minutes": 5,
        "digest_at": "07:00",
        "overdue_at": "09:00",
        "overdue_for_minutes": 60,
        "max_overdue": 10,
    }
    return {**base, **extra}


def pushes(at: str, *tasks: dict[str, Any], **extra: Any) -> list[tuple[str, str]]:
    _, intents = reminders(params(at, **extra), collected(*tasks))
    return [(i["title"], i["body"]) for i in intents]


def bodies(at: str, *tasks: dict[str, Any], **extra: Any) -> list[str]:
    return [body for _, body in pushes(at, *tasks, **extra)]


REMINDING = {"due": TODAY, "reminder": {"date": None, "time": "14:30"}}
BECAME_OVERDUE = {"due": YESTERDAY}
LONG_OVERDUE = {"due": "2026-11-01"}

CLOSED_SHAPES = [
    {"status": "done", "status_char": "x"},
    {"status": "done", "status_char": "X"},
    {"status": "cancelled", "status_char": "-"},
    # A status word and character that disagree: either one closes it.
    {"status": "done", "status_char": " "},
    {"status": "todo", "status_char": "x"},
    # A theme's status the source was not told is open.
    {"status": "custom", "status_char": ">"},
    {"status": "todo", "status_char": ""},
    {"status": "todo", "status_char": None},
]


# --- open tasks only ------------------------------------------------------------------------


@pytest.mark.parametrize("shape", CLOSED_SHAPES)
@pytest.mark.parametrize("at", ["07:00", "09:00", "14:30"])
def test_a_closed_task_never_reminds_whatever_the_source_returns(
    shape: dict[str, Any], at: str
) -> None:
    closed = [
        task("Closed reminder", **REMINDING, **shape),
        task("Closed overdue", **BECAME_OVERDUE, **shape),
        task("Closed today", due=TODAY, **shape),
    ]
    assert pushes(at, *closed) == []


def test_a_custom_status_the_source_counts_as_open_reminds() -> None:
    forwarded = task("Forwarded", status="custom", status_char=">", **REMINDING)
    _, intents = reminders(params("14:30"), collected(forwarded, open_statuses=" />"))
    assert [i["body"] for i in intents] == ["Forwarded"]
    assert pushes("14:30", forwarded) == []  # default open statuses: not open


def test_in_progress_tasks_remind() -> None:
    assert pushes("14:30", task("Half done", status="in_progress", status_char="/", **REMINDING))


# --- reminders and the lookback -------------------------------------------------------------


def test_a_reminder_is_titled_with_its_date_and_time() -> None:
    assert pushes("14:30", task("Call", **REMINDING)) == [(f"⏰ {TODAY} 14:30", "Call")]
    pick_up = task("Pick up", reminder={"date": TODAY, "time": "14:30"})
    assert pushes("14:30", pick_up) == [(f"⏰ {TODAY} 14:30", "Pick up")]


def test_the_default_lookback_is_an_hour_so_a_missed_run_is_caught_up() -> None:
    call_task = task("Call", **REMINDING)
    late = {k: v for k, v in params("15:25").items() if k != "window_minutes"}
    _, intents = reminders(late, collected(call_task))
    assert [i["title"] for i in intents] == [f"⏰ {TODAY} 14:30"]
    later = {**late, "now": params("15:31")["now"]}
    assert reminders(later, collected(call_task))[1] == []
    assert parse_params(late).window_minutes == 60


def test_the_window_is_left_open_and_right_closed_to_the_minute() -> None:
    reminder = task("Call", **REMINDING)
    assert pushes("14:25", reminder) == []
    assert pushes("14:30", reminder)
    assert pushes("14:35", reminder) == []
    now = datetime(2026, 11, 9, 13, 30, 59, tzinfo=UTC).isoformat()
    _, intents = reminders({**params("14:30"), "now": now}, collected(reminder))
    assert intents  # a run a few seconds late keeps its window


def test_a_daily_recurring_reminder_is_a_new_push_every_day() -> None:
    first = task("Stretch", due=TODAY, reminder={"date": None, "time": "08:00"}, recurring=True)
    second = {**first, "due": "2026-11-10"}
    _, one = reminders(params("08:00"), collected(first))
    _, two = reminders(params("08:00", day="2026-11-10"), collected(second))
    assert one[0]["title"] != two[0]["title"]
    assert one[0]["body"] == two[0]["body"] == "Stretch"


def test_a_bare_reminder_time_without_a_due_date_never_fires() -> None:
    assert pushes("14:30", task("No day", reminder={"date": None, "time": "14:30"})) == []


def test_a_reminder_at_midnight_fires() -> None:
    midnight = task("Midnight", due=TODAY, reminder={"date": None, "time": "00:00"})
    assert pushes("00:00", midnight) == [(f"⏰ {TODAY} 00:00", "Midnight")]


# --- uniqueness -----------------------------------------------------------------------------


def test_identical_pushes_get_a_stable_suffix_from_their_key() -> None:
    one = task("Water", **REMINDING)
    two = {**task("Water", **REMINDING), "key": "obsidian:line:Other.md:3"}
    first = bodies("14:30", one, two)
    assert len(set(first)) == 2
    assert all(body.startswith("Water · ") and len(body) == len("Water · 3fa2") for body in first)
    assert bodies("14:30", one, two) == first  # stable
    assert bodies("14:30", one) == ["Water"]  # alone: no suffix


# --- stable keys ----------------------------------------------------------------------------

KEY_SHAPE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def keys(at: str, *tasks: dict[str, Any], **extra: Any) -> list[str]:
    _, intents = reminders(params(at, **extra), collected(*tasks))
    return [i["key"] for i in intents]


def test_every_push_carries_a_key_in_the_core_alphabet() -> None:
    remind = task("Water", **REMINDING)
    late = task("Late", **BECAME_OVERDUE)
    for at in ("07:00", "09:00", "14:30"):
        found = keys(at, remind, late)
        assert found
        assert all(KEY_SHAPE.match(key) for key in found), found
    assert keys("07:00", remind) == ["digest:2026-11-09"]
    (reminder,) = keys("14:30", remind)
    assert re.fullmatch(r"rem:[0-9a-f]{12}:2026-11-09T14:30", reminder)
    nudge = [k for k in keys("09:00", late) if k.startswith("od:")]
    assert len(nudge) == 1
    assert re.fullmatch(r"od:[0-9a-f]{12}:2026-11-08", nudge[0])


def test_keys_are_stable_across_runs_and_distinct_per_task() -> None:
    one = task("Water", **REMINDING)
    two = {**task("Water", **REMINDING), "key": "obsidian:line:Other.md:3"}
    first = keys("14:30", one, two)
    assert len(set(first)) == 2
    assert keys("14:30", one, two) == first
    # A later run in the same window proposes the same key, and so does a changed body.
    assert keys("14:31", one, two) == first
    assert keys("14:30", {**one, "description": "Water the plants"}) == [first[0]]
    # The nudges differ per task too, and by the due date.
    late = task("Late", **BECAME_OVERDUE)
    other = {**task("Other", **BECAME_OVERDUE)}
    nudges = keys("09:00", late, other)
    assert len(set(nudges)) == 2
    assert keys("09:00", late, other) == nudges


def test_a_new_reminder_time_is_a_new_key() -> None:
    at_two = task("Call", due=TODAY, reminder={"date": None, "time": "14:30"})
    at_three = {**at_two, "reminder": {"date": None, "time": "15:00"}}
    assert keys("14:30", at_two) != keys("15:00", at_three)


# --- snoozed --------------------------------------------------------------------------------


def test_snoozed_is_recomputed_not_read_from_the_source() -> None:
    # The source says snoozed, but the reminder is in the past: it is not.
    stale = task("Stale flag", **BECAME_OVERDUE, snoozed=True)
    assert bodies("09:00", stale) == ["Stale flag"]
    # The source says not snoozed, but the reminder is tomorrow: it is.
    hidden = task("Tomorrow", **BECAME_OVERDUE, reminder={"date": "2026-11-10", "time": None})
    assert bodies("09:00", hidden) == []
    assert bodies("07:00", hidden) == []


def test_a_date_time_reminder_today_keeps_the_task_in_the_digest() -> None:
    today = task("Afternoon call", due=TODAY, reminder={"date": TODAY, "time": "16:00"})
    overdue_snoozed = task("Snoozed", **LONG_OVERDUE, reminder={"date": TODAY, "time": "17:00"})
    (digest,) = pushes("07:00", today, overdue_snoozed)
    assert digest[1] == "Today: 2\n- 16:00 Afternoon call\n- 17:00 Snoozed"


def test_a_future_date_only_reminder_hides_a_task_not_due_today() -> None:
    hidden = task("Next week", **LONG_OVERDUE, reminder={"date": "2026-11-16", "time": None})
    due_today = task("Due today", due=TODAY, reminder={"date": "2026-11-16", "time": None})
    (digest,) = pushes("07:00", hidden, due_today)
    assert digest[1] == "Today: 1\n- Due today"


def test_a_date_only_reminder_lists_the_task_on_its_day() -> None:
    woken = task("Woken today", reminder={"date": TODAY, "time": None}, due="2026-11-20")
    assert pushes("07:00", woken) == [(f"Today, {TODAY}", "Today: 1\n- Woken today")]


# --- the digest -----------------------------------------------------------------------------


def test_the_digest_sections_and_order() -> None:
    tasks = [
        task("Book the train", due=TODAY),
        task("Call the plumber", **REMINDING),
        task("Renew the passport", **BECAME_OVERDUE),
        task("Send the form", due="2026-11-05"),
        task("Not today", due="2026-11-20"),
    ]
    (digest,) = pushes("07:00", *tasks)
    assert digest == (
        f"Today, {TODAY}",
        "Overdue: 2\n"
        "- Send the form (due 2026-11-05)\n"
        "- Renew the passport (due 2026-11-08)\n"
        "Today: 2\n"
        "- 14:30 Call the plumber\n"
        "- Book the train",
    )


def test_the_digest_lists_the_oldest_max_overdue() -> None:
    old = [task(f"Old {n:02}", due=f"2026-10-{n + 1:02}") for n in range(15)]
    (digest,) = pushes("07:00", *reversed(old), max_overdue=3)
    assert digest[1].split("\n") == [
        "Overdue: 15",
        "- Old 00 (due 2026-10-01)",
        "- Old 01 (due 2026-10-02)",
        "- Old 02 (due 2026-10-03)",
        "- …and 12 more",
    ]


def test_the_digest_keeps_to_its_byte_budget_and_never_cuts_a_line() -> None:
    wide = "日本語のタスク " * 30  # 3 bytes a character
    tasks = [task(f"{wide}{n}", due=TODAY) for n in range(60)]
    _, intents = reminders(params("07:00"), collected(*tasks, more=4))
    (digest,) = intents
    body = digest["body"]
    lines = body.split("\n")
    assert len(body.encode("utf-8")) <= DIGEST_BYTES
    assert lines[-1] == UNREAD_LINE
    assert lines[-2].startswith("…and ")
    assert lines[-2].endswith(" more")
    shown = [line for line in lines if line.startswith("- ")]
    assert int(lines[-2].split()[1]) == 60 - len(shown)
    for line in shown:
        assert line.endswith("…")  # each description cut on a word boundary, never a line


@pytest.mark.parametrize(
    "data",
    [
        {"more": 2},
        {"output_cut": True},
        {"withheld": [{"key": "k", "path": "a.md", "line": 1, "reason": "duplicate_key"}]},
        {"withheld_more": 1},
        {"hidden": [{"path": "a.md", "line": 1, "reason": "owned_block"}]},
        {"skipped": [{"path": "big.md", "reason": "too_large"}]},
    ],
)
def test_the_digest_says_when_tasks_could_not_be_read(data: dict[str, Any]) -> None:
    result, intents = reminders(params("07:00"), collected(task("T", due=TODAY), **data))
    assert intents[0]["body"] == f"Today: 1\n- T\n{UNREAD_LINE}"
    assert (
        result["more"] + result["withheld"] + result["hidden"] + result["skipped"]
        or (result["output_cut"])
    )


def test_no_digest_on_an_empty_day() -> None:
    assert pushes("07:00", task("Later", due="2026-11-20")) == []


def test_the_digest_is_proposed_by_every_run_in_the_lookback_with_the_same_text() -> None:
    tasks = [task("Book", due=TODAY)]
    runs = [pushes(at, *tasks, window_minutes=60) for at in ("06:55", "07:00", "07:55", "08:00")]
    assert runs[0] == runs[3] == []
    assert runs[1] == runs[2] == [(f"Today, {TODAY}", "Today: 1\n- Book")]


def test_a_digest_after_midnight_is_caught_up_by_the_next_day_s_first_run() -> None:
    tasks = [task("Book", due=TODAY)]
    caught = pushes("00:10", *tasks, day="2026-11-10", digest_at="23:30", window_minutes=60)
    assert caught == [(f"Today, {TODAY}", "Today: 1\n- Book")]


def test_quiet_hour_reminders_are_left_out_of_the_digest() -> None:
    early = task("Early", due=TODAY, reminder={"date": None, "time": "06:30"})
    late = task("Late", due=TODAY, reminder={"date": None, "time": "09:00"})
    (digest,) = pushes("07:00", early, late, quiet_hours=QUIET)
    assert digest[1] == "Today: 1\n- 09:00 Late"
    # Without quiet hours (or with them elsewhere) it is listed.
    assert "Early" in pushes("07:00", early, late)[0][1]
    elsewhere = {"start": "23:00", "end": "06:00"}
    assert "Early" in pushes("07:00", early, late, quiet_hours=elsewhere)[0][1]


DROP = {**QUIET, "during": "drop"}


def test_with_drop_a_night_reminder_is_in_the_digest_and_has_no_push_of_its_own() -> None:
    night = task("Night", due=TODAY, reminder={"date": None, "time": "03:00"})
    late = task("Late", due=TODAY, reminder={"date": None, "time": "09:00"})
    # Proposed at 03:00 (quiet): nothing to drop, so nothing proposed.
    assert pushes("03:00", night, late, quiet_hours=DROP) == []
    # Still nothing when the run is the one after: no individual push in quiet hours.
    (digest,) = pushes("07:00", night, late, quiet_hours=DROP)
    assert digest[1] == "During quiet hours: 1\n- ⏰ 03:00 Night\nToday: 1\n- 09:00 Late"
    # `delay` (explicit or default) keeps today's behaviour: its own delayed push.
    delayed = {**QUIET, "during": "delay"}
    assert pushes("03:00", night, quiet_hours=delayed)[0][0] == f"⏰ {TODAY} 03:00"
    assert pushes("07:00", night, late, quiet_hours=delayed)[0][1] == "Today: 1\n- 09:00 Late"


def test_with_drop_only_the_night_since_the_previous_digest_is_listed() -> None:
    old = task("Old", due=YESTERDAY, reminder={"date": None, "time": "23:00"})
    (digest,) = pushes("07:00", old, quiet_hours=DROP)
    assert digest[1] == "During quiet hours: 1\n- ⏰ 23:00 Old"
    # A reminder before the previous digest (yesterday 06:00 is quiet, but 25h earlier) is not.
    older = task("Older", reminder={"date": YESTERDAY, "time": "06:00"}, due=YESTERDAY)
    assert "quiet" not in pushes("07:00", older, quiet_hours=DROP)[0][1]


def test_with_drop_a_daytime_reminder_is_still_pushed() -> None:
    noon = task("Noon", due=TODAY, reminder={"date": None, "time": "12:00"})
    assert pushes("12:00", noon, quiet_hours=DROP) == [(f"⏰ {TODAY} 12:00", "Noon")]


def test_quiet_hours_during_is_validated() -> None:
    with pytest.raises(ReminderError, match="during"):
        parse_params(params("07:00", quiet_hours={**QUIET, "during": "mute"}))
    assert parse_params(params("07:00", quiet_hours=DROP)).quiet_drops
    assert not parse_params(params("07:00", quiet_hours=QUIET)).quiet_drops


# --- overdue nudges -------------------------------------------------------------------------


def test_only_tasks_that_became_overdue_are_nudged() -> None:
    tasks = [task("Yesterday", **BECAME_OVERDUE), task("Long ago", **LONG_OVERDUE)]
    data, intents = reminders(params("09:00"), collected(*tasks))
    assert [(i["title"], i["body"]) for i in intents] == [("Overdue: due 2026-11-08", "Yesterday")]
    assert data["overdue"] == 2
    assert data["overdue_nudges"] == 1


def test_overdue_nudges_are_capped() -> None:
    tasks = [task(f"Late {n:02}", **BECAME_OVERDUE) for n in range(15)]
    data, intents = reminders(params("09:00"), collected(*tasks))
    assert len(intents) == 10
    assert data["overdue_nudges"] == 10


def test_overdue_nudges_hold_for_their_hour_with_the_same_text() -> None:
    late = task("Pay the bill", **BECAME_OVERDUE)
    assert pushes("08:55", late) == []
    assert (
        pushes("09:00", late)
        == pushes("09:55", late)
        == [("Overdue: due 2026-11-08", "Pay the bill")]
    )
    assert pushes("10:00", late) == []


def test_a_nudge_window_across_midnight() -> None:
    late = task("Pay the bill", due="2026-11-07")
    nudged = pushes("00:10", late, overdue_at="23:30", overdue_for_minutes=60)
    assert nudged == [("Overdue: due 2026-11-07", "Pay the bill")]


def test_the_text_is_stable_across_runs() -> None:
    tasks = [task("Pay the bill", **BECAME_OVERDUE), task("Call", **REMINDING)]
    nine, nine_thirty = pushes("09:00", *tasks), pushes("09:30", *tasks)
    assert nine == nine_thirty
    at_reminder = pushes("14:30", *tasks, overdue_at="14:00")
    assert at_reminder == [(f"⏰ {TODAY} 14:30", "Call"), *nine]
    for title, body in at_reminder:
        assert "min" not in title
        assert "09:" not in title + body


# --- input and parameters -------------------------------------------------------------------


def test_a_malformed_task_is_skipped_and_counted() -> None:
    good = task("Call", **REMINDING)
    bad = [
        task("Bad due", due="2026-13-01", reminder={"date": None, "time": "14:30"}),
        task("Bad time", due=TODAY, reminder={"date": None, "time": "25:00"}),
        task("Bad reminder", due=TODAY, reminder="14:30"),
        task("Bad date type", due=20261109),
    ]
    data, intents = reminders(params("14:30"), collected(good, *bad, {"not": "a task"}))
    assert [i["body"] for i in intents] == ["Call"]
    assert data["invalid_tasks"] == 4


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [{"source": "calendar.events", "params": {}, "data": {}}],
        [{"source": SOURCE, "params": {}, "data": {"tasks": "none"}}],
        [{"source": SOURCE, "params": {}, "data": {"tasks": [], "open_statuses": 3}}],
    ],
)
def test_an_input_without_open_tasks_fails_the_call(value: Any) -> None:
    with pytest.raises(ReminderError):
        reminders(params("09:00"), value)


@pytest.mark.parametrize(
    "bad",
    [
        {"now": "2026-11-09T09:00:00"},  # no offset
        {"now": "yesterday"},
        {"now": None},
        {"timezone": "Mars/Olympus"},
        {"timezone": ""},
        {"window_minutes": 0},
        {"window_minutes": 721},
        {"window_minutes": True},
        {"window_minutes": "5"},
        {"digest_at": "7:00"},
        {"overdue_at": "24:00"},
        {"max_overdue": 101},
        {"quiet_hours": "22:00-07:00"},
        {"quiet_hours": {"start": "22:00"}},
        {"quiet_hours": {"start": "22:00", "end": "22:00"}},
        {"quiet_hours": {"start": "22:00", "end": "07:00", "extra": 1}},
        {"window_minute": 5},  # a typo is an error, never a silently ignored setting
    ],
)
def test_invalid_parameters_fail_the_call(bad: dict[str, Any]) -> None:
    with pytest.raises(ReminderError):
        parse_params({**params("09:00"), **bad})


def test_optional_parameters() -> None:
    parsed = parse_params({"now": "2026-11-09T08:00:00Z", "timezone": PARIS})
    assert (parsed.window_minutes, parsed.max_overdue) == (60, 10)
    assert (parsed.digest_at, parsed.overdue_at, parsed.quiet_hours) == (None, None, None)
    tasks = [task("Pay", **BECAME_OVERDUE), task("Today", due=TODAY)]
    for now in ("2026-11-09T06:00:00Z", "2026-11-09T08:00:00Z"):
        _, intents = reminders({"now": now, "timezone": PARIS}, collected(*tasks))
        assert intents == []


# --- text -----------------------------------------------------------------------------------


def test_clean_makes_one_bounded_line_of_plain_text() -> None:
    assert clean("Pay\nthe\x07bill\u2028now") == "Pay the bill now"
    assert clean("") == clean(None) == "(no description)"
    assert clean("Read [the doc](https://x.test/a) and [[Note|this]] or [[Other]]") == (
        "Read the doc and this or Other"
    )
    assert clean("Call #work/home the #urgent plumber about #42") == "Call the plumber about #42"
    long = clean("word " * 100)
    assert len(long) <= MAX_DESCRIPTION
    assert long.endswith("word…")
    unbroken = clean("x" * 1000)
    assert len(unbroken) == MAX_DESCRIPTION
    assert unbroken.endswith("…")


def test_the_link_horae_wrote_is_not_in_the_push() -> None:
    link = "https://github.com/horae-dev/horae/pull/1"
    written = task("Review the PR [link](" + link + ")", **REMINDING, link=link)
    assert bodies("14:30", written) == ["Review the PR"]


# --- daylight saving ------------------------------------------------------------------------


def test_instant_follows_cron_rules_on_daylight_saving_days() -> None:
    # Skipped (spring forward, 02:00 → 03:00 CEST): the first instant after the gap.
    assert instant(date(2026, 3, 29), time(2, 30), ZONE) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    # Repeated (fall back, 03:00 CEST → 02:00 CET): the first occurrence.
    assert instant(date(2026, 10, 25), time(2, 30), ZONE) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )
    assert instant(date(2026, 11, 9), time(7, 0), ZONE) == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


def _run_day(day: str, *tasks: dict[str, Any]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Every 5 minutes (UTC) of `day` for 9 hours from 00:00 Paris time, window 5."""
    start = datetime.fromisoformat(f"{day} 00:00").replace(tzinfo=ZONE).astimezone(UTC)
    out = []
    for step in range(9 * 12):
        moment = start + step * (datetime(2000, 1, 1, 0, 5) - datetime(2000, 1, 1))
        run = {**params("00:00"), "now": moment.isoformat()}
        _, intents = reminders(run, collected(*tasks))
        if intents:
            local = moment.astimezone(ZONE).strftime("%H:%M")
            out.append((local, [(i["title"], i["body"]) for i in intents]))
    return out


def test_spring_forward_a_skipped_reminder_fires_once_after_the_gap() -> None:
    boiler = task("Check the boiler", due="2026-03-29", reminder={"date": None, "time": "02:30"})
    assert _run_day("2026-03-29", boiler) == [
        ("03:00", [("⏰ 2026-03-29 02:30", "Check the boiler")]),
        ("07:00", [("Today, 2026-03-29", "Today: 1\n- 02:30 Check the boiler")]),
    ]


def test_fall_back_a_repeated_reminder_fires_once() -> None:
    boiler = task("Check the boiler", due="2026-10-25", reminder={"date": None, "time": "02:30"})
    fired = _run_day("2026-10-25", boiler)
    assert [at for at, _ in fired] == ["02:30", "07:00"]
    assert fired[0][1] == [("⏰ 2026-10-25 02:30", "Check the boiler")]


# --- through the plugin, and the shared vectors ---------------------------------------------


def test_the_plugin_proposes_the_pushes() -> None:
    plugin = build()
    result = anyio.run(
        lambda: call(
            plugin,
            "obsidian.reminders",
            params("14:30"),
            input=collected(task("Call", **REMINDING)),
        )
    )
    assert result.error is None
    (intent,) = result.intents
    assert intent == {
        "type": "notify.push",
        "title": f"⏰ {TODAY} 14:30",
        "body": "Call",
        "key": intent["key"],
    }
    assert intent["key"].startswith("rem:") and intent["key"].endswith(f":{TODAY}T14:30")
    assert result.data["reminders"] == 1


def test_the_plugin_reports_a_bad_parameter_as_a_tool_error() -> None:
    plugin = build()
    result = anyio.run(
        lambda: call(
            plugin,
            "obsidian.reminders",
            {**params("14:30"), "timezone": "Nowhere"},
            input=collected(),
        )
    )
    assert result.error is not None
    assert "timezone" in result.error


def test_the_shared_vectors() -> None:
    """`horae_obsidian/vectors/reminders.json`: the golden vectors pack-reminders reuses."""
    vectors = reminder_vectors()
    assert len(vectors) >= 20
    for vector in vectors:
        if "error" in vector:
            with pytest.raises(ReminderError):
                reminders(vector["params"], vector["input"])
            continue
        data, intents = reminders(vector["params"], vector["input"])
        assert intents == vector["intents"], vector["name"]
        for name, value in vector.get("data", {}).items():
            assert data[name] == value, (vector["name"], name)
        json.dumps(vector)


def test_a_digest_full_of_overdue_tasks_counts_the_rest() -> None:
    wide = "日本語のタスク " * 30
    late = [task(f"{wide}{n}", due="2026-11-01") for n in range(40)]
    today = [task("Midnight", due=TODAY, reminder={"date": None, "time": "00:00"})]
    _, intents = reminders(params("07:00", max_overdue=100), collected(*late, *today))
    lines = intents[0]["body"].split("\n")
    shown = [line for line in lines if line.startswith("- ")]
    assert lines[0] == "Overdue: 40"
    assert "Today: 1" not in lines
    assert lines[-1] == f"…and {41 - len(shown)} more"
    (midnight,) = pushes("07:00", *today)
    assert midnight[1] == "Today: 1\n- 00:00 Midnight"
