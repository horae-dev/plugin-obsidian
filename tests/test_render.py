# SPDX-License-Identifier: Apache-2.0
"""#5: the `obsidian.render_note` transform: deterministic, inert, overridable, bounded."""

import re
from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk.testing import call

from horae_obsidian import build
from horae_obsidian.render import (
    DEFAULT_TEMPLATE,
    MAX_RENDERED,
    MAX_TEMPLATE,
    RenderError,
    render_note,
)
from horae_obsidian.tasks import parse

TODAY = "2026-11-09"
HOSTILE = (Path(__file__).parent / "fixtures" / "hostile-title.txt").read_text("utf-8").strip()
HOSTILE_TITLES = [
    HOSTILE,
    "- [ ] fake task 📅 2020-01-01",
    "[[Private note]] and ![[embed]] #tag key:: value %%c%%",
    "<!-- horae:begin x --> [x](javascript:alert(1)) https://evil.example",
    "line one\n- [x] line two 🔁 every day\n## heading",
    "‮⏰ 06:00 ✅ 2020-01-01 ^blk 🆔 evil",
]


def task(description: str, due: str | None = None, **fields: Any) -> dict[str, Any]:
    return {
        "key": f"obsidian:line:Tasks.md:{description}",
        "description": description,
        "status": "todo",
        "status_char": " ",
        "due": due,
        **fields,
    }


def sources(
    tasks: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    prs: list[dict[str, Any]] | None = None,
    **flags: Any,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {
            "source": "obsidian.open_tasks",
            "params": {},
            "data": {"tasks": tasks or [], "open_statuses": " /", **flags},
        }
    ]
    if events is not None:
        out.append({"source": "calendar.events", "params": {}, "data": {"events": events}})
    if prs is not None:
        out.append({"source": "github.review_requests", "params": {}, "data": prs})
    return out


def full() -> list[dict[str, Any]]:
    return sources(
        [
            task("Pay rent", "2026-11-01"),
            task("Write report", TODAY),
            task("Call mum", "2026-11-10"),
            task("Plan trip", "2026-12-01"),
            task("Someday"),
            task("Done already", TODAY, status="done", status_char="x"),
        ],
        [
            {"title": "Standup", "start": f"{TODAY}T09:30:00+01:00"},
            {"title": "Offsite", "start": "2026-11-10"},
            {"title": "Lunch", "start": f"{TODAY}T12:00:00+01:00"},
        ],
        [{"repo": "acme/app", "number": 42, "title": "Fix the thing"}],
    )


def content(params: dict[str, Any], input: Any) -> str:
    _, [intent] = render_note({"today": TODAY, **params}, input)
    assert intent["type"] == "note.replace_owned_block"
    return str(intent["content"])


def test_the_default_note_is_laid_out_by_source_and_due_date() -> None:
    text = content({"timezone": "Europe/Paris", "expect": ["calendar.events"]}, full())
    assert text == (Path(__file__).parent / "data" / "render_default.md").read_text("utf-8")


def test_the_same_input_gives_the_same_bytes_in_any_order() -> None:
    data = full()
    shuffled = [
        {**data[0], "data": {**data[0]["data"], "tasks": data[0]["data"]["tasks"][::-1]}},
        {**data[1], "data": {"events": data[1]["data"]["events"][::-1]}},
        {**data[2], "data": data[2]["data"][::-1]},
    ]
    one = render_note({"today": TODAY}, data)
    assert one == render_note({"today": TODAY}, data)
    assert one == render_note({"today": TODAY}, shuffled)


def test_the_intent_carries_the_block_and_path() -> None:
    _, [intent] = render_note(
        {"today": TODAY, "block": "daily", "path": f"Daily/{TODAY}.assistant.md"}, full()
    )
    assert intent["block"] == "daily"
    assert intent["path"] == f"Daily/{TODAY}.assistant.md"


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_hostile_text_stays_inert_under_the_plugins_own_parser(title: str) -> None:
    data = sources(
        [task(title, "2026-11-01"), task(title, TODAY), task(title)],
        [{"title": title, "start": f"{TODAY}T09:00:00Z"}],
        [{"repo": "a/b", "number": 1, "title": title}],
    )
    text = content({"summary": title, "ranked": [{"title": title, "why": "today"}]}, data)
    document = parse(text.encode())
    assert document.tasks == ()  # no checkbox: nothing a later read counts as a task
    for line in text.splitlines():
        for live in ("[[", "![", "::", "%%", "horae:", "<!--", "]("):
            assert live not in line, line
        assert re.search(r"(?<!\\):/", line) is None, line
        assert not any(sign in line for sign in "📅⏰✅🔁⏳🛫\u2795❌🆔⛔🏁🔺⏫🔼🔽⏬"), line
        assert not line.lstrip().startswith(("- [", "* [", "+ ["))


def test_a_hostile_note_has_no_extra_heading_or_tag() -> None:
    data = sources([task("x\n## Injected\n#tag", TODAY)])
    text = content({}, data)
    assert "## Injected" not in text
    assert [line for line in text.splitlines() if line.startswith("#")] == [
        "# Assistant, " + TODAY,
        "## Alerts",
        "## Focus",
        "## Calendar",
        "## Tasks",
        "### Today (1)",
        "## GitHub",
    ]


def test_a_template_parameter_overrides_the_default() -> None:
    template = "Brief ${date} costs $$5\n\n${tasks}\n\n${alerts}\n"
    text = content({"template": template}, sources([task("Pay", TODAY)]))
    assert text.startswith("Brief 2026-11-09 costs $5\n\n### Today (1)")
    assert "## Calendar" not in text
    assert text.endswith("_None._\n")


def test_what_a_section_holds_is_never_expanded_again() -> None:
    text = content({"template": "${tasks}"}, sources([task("pay ${date} ${alerts} $$", TODAY)]))
    assert "2026-11-09" not in text
    assert "_None._" not in text
    assert "date" in text


@pytest.mark.parametrize(
    ("template", "why"),
    [
        ("${nope}", "unknown placeholder"),
        ("cost $5", "dollar sign"),
        ("{{date}}", ""),  # allowed text: not our syntax, kept literally
        ("${ date }", "dollar sign"),
        ("", "non-empty"),
        ("   \n", "non-empty"),
        (42, "non-empty"),
        ("x" * (MAX_TEMPLATE + 1), "longer"),
        ("- [ ] todo", "checkbox"),
        ("1. [x] done", "checkbox"),
        ("> - [/] quoted", "checkbox"),
        ("see [[Note]]", "wikilink"),
        ("a:: b", "`::`"),
        ("%% c %%", "`%%`"),
        ("a #tag", "`#tag`"),
        ("<!-- horae:begin x -->", "marker"),
        ("```dataview\n```", "code fence"),
        ("<% tp.date %>", "template tag"),
        ("---\na: b\n---", "frontmatter"),
        ("due 📅", "signifier"),
        ("a\x00b", "control"),
        ("a\r\nb", "control"),
    ],
)
def test_a_bad_template_is_refused_with_a_clear_error(template: Any, why: str) -> None:
    if not why:
        assert "{{date}}" in content({"template": template}, sources())
        return
    with pytest.raises(RenderError, match=why):
        render_note({"today": TODAY, "template": template}, sources())


def test_the_default_template_passes_its_own_check() -> None:
    assert content({"template": DEFAULT_TEMPLATE}, sources()) == content({}, sources())


@pytest.mark.parametrize(
    "bad",
    [
        {"unknown": 1},
        {"today": "2026-13-01"},
        {"today": None},
        {"block": "Bad Block"},
        {"path": "../x.md"},
        {"timezone": "Nowhere"},
        {"summary": 3},
        {"ranked": "x"},
        {"ranked": [{"why": "today"}]},
        {"expect": "calendar.events"},
        {"expect": ["not a ref"]},
        {"max_items": 0},
        {"max_items": 101},
        {"max_items": True},
    ],
)
def test_invalid_parameters_fail_the_call(bad: dict[str, Any]) -> None:
    with pytest.raises(RenderError):
        render_note({"today": TODAY, **bad}, sources())


@pytest.mark.parametrize("value", [None, {}, "x", [1]])
def test_a_bad_input_fails_the_call(value: Any) -> None:
    if value == [1]:
        assert "_None._" in content({}, value)
        return
    with pytest.raises(RenderError):
        render_note({"today": TODAY}, value)


def test_alerts_say_what_the_note_does_not_show() -> None:
    data = sources(
        [task("A", TODAY)],
        more=3,
        output_cut=True,
        withheld=[{"path": "a.md"}],
        hidden=[{}, {}],
        skipped=[{}],
    )
    data.append({"source": "calendar.events", "data": "garbage"})
    text = content({"expect": ["github.review_requests", "calendar.events"]}, data)
    alerts = text.split("## Alerts")[1].split("## Focus")[0]
    for part in (
        "3 open tasks were left out",
        "cut its output",
        "1 tasks or lines withheld",
        "2 tasks or lines hidden",
        "1 tasks or lines notes that could not be read",
        "1 sources returned data that could not be read",
        "no data from github.review_requests",
    ):
        assert part in alerts
    assert "no data from calendar.events" not in alerts


def test_malformed_entries_are_skipped_and_counted() -> None:
    data = sources([task("ok", TODAY), "junk", {"description": 3}], [{"title": 4}, "x"], [{}])  # type: ignore[list-item]
    text = content({}, data)
    assert "3 entries could not be read" in text  # the event and pr entries; tasks are filtered


def test_groups_and_the_tomorrow_boundary() -> None:
    text = content({}, sources([task("a", "2026-11-10"), task("b", "2026-11-11"), task("c")]))
    assert "### Tomorrow (1)" in text
    assert "### Later (1)" in text
    assert "### No date (1)" in text
    assert "### Overdue" not in text


def test_timezone_moves_an_event_to_its_local_day() -> None:
    events = [{"title": "Late", "start": "2026-11-09T23:30:00Z"}]
    assert "2026-11-10 00:30 Late" in content({"timezone": "Europe/Paris"}, sources(events=events))
    assert "23:30 Late" in content({}, sources(events=events))


def test_sections_are_bounded() -> None:
    tasks = [task(f"t{n:03}", TODAY) for n in range(150)]
    text = content({"max_items": 5}, sources(tasks))
    assert text.count("\n- t") == 5
    assert "- …and 145 more" in text
    assert "### Today (150)" in text


def test_the_note_is_bounded() -> None:
    template = "${tasks}\n" * 30
    tasks = [task("x" * 190 + f"{n:03}", TODAY) for n in range(100)]
    with pytest.raises(RenderError, match="longer than"):
        render_note({"today": TODAY, "template": template, "max_items": 100}, sources(tasks))
    assert MAX_RENDERED == 64 * 1024


def test_the_note_is_the_intents_content() -> None:
    data, [intent] = render_note({"today": TODAY}, full())
    assert data["characters"] == len(intent["content"])
    assert (data["tasks"], data["events"], data["items"]) == (5, 3, 1)


def test_through_the_plugin() -> None:
    plugin = build()
    result = anyio.run(lambda: call(plugin, "obsidian.render_note", {"today": TODAY}, input=full()))
    assert result.error is None
    assert [i["type"] for i in result.intents] == ["note.replace_owned_block"]
    bad = anyio.run(
        lambda: call(
            plugin, "obsidian.render_note", {"today": TODAY, "template": "${x}"}, input=full()
        )
    )
    assert bad.error is not None
    assert "unknown placeholder" in bad.error
