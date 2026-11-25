# SPDX-License-Identifier: Apache-2.0
"""`obsidian.render_note`'s `${github}` section, fed plugin-github 0.5.0's recorded data
(`tests/data/github/*.json`: the `expect.data` of plugin-github's `tests/recordings`, all
synthetic): linked references, the event and its time, failing checks, newest first, the
source's own flags as alerts, and hostile titles, logins, branches and links kept inert."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from horae_sdk.tasks import SIGNIFIERS

from horae_obsidian.render import render_note
from horae_obsidian.tasks import parse

DATA = Path(__file__).parent / "data" / "github"
TODAY = "2026-10-29"
TEMPLATE = "${alerts}\n\n${github}\n"
LINK = re.compile(r"^- \[([A-Za-z0-9_.\\/-]+(?:\\#\d+)?)\]\((https://github\.com/[^\s()]+)\) ")


def recorded(name: str) -> dict[str, Any]:
    entry: dict[str, Any] = json.loads((DATA / f"{name}.json").read_text("utf-8"))
    return entry


def note(*entries: dict[str, Any], **params: Any) -> str:
    _, [intent] = render_note(
        {"today": TODAY, "template": TEMPLATE, "timezone": "Europe/Paris", **params},
        list(entries),
    )
    return str(intent["content"])


def item(**fields: Any) -> dict[str, Any]:
    return {"source": "github.review_requests", "data": {"items": [fields]}}


@pytest.mark.parametrize("name", ["ci-failures", "updates"])
def test_a_recorded_source_renders_as_its_golden_note(name: str) -> None:
    assert note(recorded(name)) == (DATA / f"{name}.md").read_text("utf-8")


def test_an_update_reads_ref_title_who_what_and_when() -> None:
    text = note(recorded("updates"))
    assert (
        "- [acme-up/app\\#4](https://github.com/acme-up/app/pull/4) Add retries"
        " — carol approved 2026-10-26 11:00\n"
    ) in text
    assert "judy requested your review 2026-10-28 11:00" in text
    assert "kim opened 11:00" in text  # today: the time alone


def test_updates_are_listed_newest_first_whatever_the_input_order() -> None:
    entry = recorded("updates")
    lines = [line for line in note(entry).splitlines() if line.startswith("- ")]
    stamps = [re.search(r"(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2})$", line) for line in lines]
    days = [(m.group(1) or TODAY, m.group(2)) for m in stamps if m]
    assert len(days) == len(lines) == 7
    assert days == sorted(days, reverse=True)
    shuffled = {**entry, "data": {**entry["data"], "items": entry["data"]["items"][::-1]}}
    assert note(shuffled) == note(entry)


def test_the_time_follows_the_timezone_parameter() -> None:
    entry = recorded("updates")
    _, [intent] = render_note({"today": TODAY, "template": TEMPLATE}, [entry])
    assert "carol approved 2026-10-26 10:00" in intent["content"]  # as written (UTC)


def test_a_ci_failure_names_the_branch_or_pr_and_the_checks() -> None:
    text = note(recorded("ci-failures"))
    assert (
        "- [acme-org/api](https://github.com/acme-org/api/actions/runs/9001) main"
        " — Nightly / build, Nightly / test failing since 2026-10-30\n"
    ) in text
    assert "[acme-org/api\\#12](https://github.com/acme-org/api/actions/runs/9100)" in text
    assert "feature/cache — Nightly / unit failing since 2026-10-29" in text
    assert "Lint (59 checks not read) failing since 2026-10-25" in text


def test_ci_failures_keep_repository_order() -> None:
    lines = [line for line in note(recorded("ci-failures")).splitlines() if line.startswith("- ")]
    repos = [LINK.match(line).group(1) for line in lines]  # type: ignore[union-attr]
    assert repos == [
        "acme-org/api",
        "acme-org/api\\#12",
        "acme-org/docs",
        "acme-user/dotfiles",
        "acme-user/dotfiles\\#3",
    ]


def test_more_checks_are_counted() -> None:
    text = note(
        {
            "source": "github.ci_failures",
            "data": {
                "items": [
                    {
                        "repo": "a/b",
                        "branch": "main",
                        "number": None,
                        "checks": ["one", "two"],
                        "more_checks": 3,
                        "url": "https://github.com/a/b/commit/abc",
                    }
                ]
            },
        }
    )
    assert "main — one, two and 3 more failing\n" in text


@pytest.mark.parametrize(
    ("name", "alert"),
    [
        ("updates-max-items", "github.updates had more items than it returned (truncated)"),
        ("ci-failures-max-items", "github.ci_failures had more items than it returned"),
        ("updates-saml", "github.updates: GitHub hid some results (1 errors)"),
        ("ci-failures-saml", "github.ci_failures: GitHub hid some results (3 errors)"),
    ],
)
def test_the_sources_flags_become_alerts(name: str, alert: str) -> None:
    alerts = note(recorded(name)).split("\n\n")[0]
    assert alert in alerts


def test_a_clean_source_raises_no_alert() -> None:
    assert note(recorded("updates"), recorded("ci-failures")).startswith("_None._\n")


@pytest.mark.parametrize(
    "url",
    [
        None,
        42,
        "",
        "http://github.com/a/b/pull/1",
        "https://github.com.evil.example/a/b/pull/1",
        "https://evil.example/a/b/pull/1",
        "https://user@github.com/a/b/pull/1",
        "https://github.com/a/b/pull/1 x",
        "https://github.com/a/b/pull/1‮",
        "https://127.0.0.1/a",
        "javascript:alert(1)",
    ],
)
def test_a_link_that_is_not_a_github_task_link_is_left_out(url: object) -> None:
    text = note(item(repo="a/b", number=1, title="Fix", url=url))
    assert text.endswith("\n- a/b\\#1 Fix\n")
    assert "](" not in text


def test_the_link_destination_is_inert() -> None:
    text = note(item(repo="a/b", number=1, title="t", url="https://github.com/a/b/pull/1?q=(x)"))
    assert "[a/b\\#1](https://github.com/a/b/pull/1?q=%28x%29) t" in text


@pytest.mark.parametrize("repo", ["a/b c", "a/b/c", "../x", "[a](b)/c", 3])
def test_a_repository_that_cannot_be_one_gets_no_reference(repo: object) -> None:
    text = note(item(repo=repo, number=7, title="t", url="https://github.com/a/b/pull/7"))
    assert text.endswith("\n- [\\#7](https://github.com/a/b/pull/7) t\n")


@pytest.mark.parametrize("name", ["updates-hostile", "ci-failures-hostile"])
def test_hostile_recordings_stay_inert(name: str) -> None:
    text = note(recorded(name))
    assert parse(text.encode()).tasks == ()
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert lines
    for line in lines:
        match = LINK.match(line)
        assert match is not None, line
        rest = line[match.end() :]
        for live in ("[[", "![", "::", "%%", "<!--", "]("):
            assert live not in rest, line
        assert re.search(r"(?<!\\):/", rest) is None, line
        assert re.search(r"(?<![\w\\&])#[^\s#]", rest) is None, line
        assert re.search(r"(?<!\\)[<>]", rest) is None, line  # no raw HTML
        assert not any(sign in rest for sign in SIGNIFIERS), line


HOSTILE = [
    "x:: y %%c%% ==hi== [[n]] ![[e]] #tag 📅 2020-01-01",
    "end:",
    ":start",
    "%",
    "=",
    "\\",
    "a\n- [ ] injected\n## Heading",
    "‮⏰ 06:00 ^blk",
    "](https://evil.example) <!-- horae:end x -->",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_pieces_never_meet_into_live_syntax(hostile: str) -> None:
    text = note(
        {
            "source": "github.updates",
            "data": {
                "items": [
                    {
                        "repo": "a/b",
                        "number": 1,
                        "item_title": hostile,
                        "event": "commented",
                        "by": hostile,
                        "at": "2026-10-29T10:00:00Z",
                        "url": "https://github.com/a/b/issues/1",
                    }
                ]
            },
        },
        {
            "source": "github.ci_failures",
            "data": {
                "items": [
                    {
                        "repo": "a/c",
                        "number": 2,
                        "branch": hostile,
                        "checks": [hostile, hostile],
                        "since": "2026-10-29",
                        "url": "https://github.com/a/c/pull/2",
                    }
                ]
            },
        },
    )
    assert parse(text.encode()).tasks == ()
    lines = [line for line in text.splitlines() if line]
    assert all(line.startswith(("- ", "_None._")) for line in lines), text
    for line in lines[1:]:
        match = LINK.match(line)
        assert match is not None, line
        rest = line[match.end() :]
        for live in ("[[", "::", "%%", "==", "<!--", "](", "horae:"):
            assert live not in rest, line


def test_items_without_a_title_or_checks_are_counted_as_unread() -> None:
    text = note({"source": "github.updates", "data": {"items": [{"repo": "a/b"}, "x"]}})
    assert "2 entries could not be read" in text


# plugin-github 0.5.2: `hidden` says why results are missing, `checks_hidden` on CI failures.


@pytest.mark.parametrize(
    ("name", "alerts"),
    [
        (
            "updates-token-lifetime",
            [
                "github.updates: acme-lifetime: hidden because the token's lifetime is over the"
                " organisation's limit (set an expiry of 366 days or less) (4)"
            ],
        ),
        (
            "ci-failures-hidden-reasons",
            [
                "github.ci_failures: acme-lifetime: hidden because the token's lifetime is over"
                " the organisation's limit (set an expiry of 366 days or less) (1)",
                "github.ci_failures: acme-missing: not readable with this token (1)",
            ],
        ),
        (
            "ci-failures-saml-hidden",
            [
                "github.ci_failures: acme-saml: hidden by SAML single sign-on"
                " (authorise the token for SSO) (1)",
                "github.ci_failures: acme-sso: hidden by SAML single sign-on"
                " (authorise the token for SSO) (1)",
                "github.ci_failures: 1 results not found by GitHub",  # errors 3, 2 explained
            ],
        ),
    ],
)
def test_hidden_recordings_give_one_alert_per_owner_and_reason(
    name: str, alerts: list[str]
) -> None:
    shown = note(recorded(name)).split("\n\n")[0]
    assert shown == "\n".join(f"- {alert}" for alert in alerts)
    assert "GitHub hid some results" not in shown


def hidden(*entries: object, **data: Any) -> dict[str, Any]:
    return {"source": "github.updates", "data": {"items": [], "hidden": list(entries), **data}}


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("token_lifetime", "hidden because the token's lifetime is over the organisation's limit"),
        ("token_not_allowed", "hidden because the organisation does not allow this token"),
        ("saml", "hidden by SAML single sign-on (authorise the token for SSO)"),
        ("ip_allow_list", "hidden by the organisation's IP allow list"),
        ("not_accessible", "not readable with this token"),
        ("forbidden", "hidden: GitHub refused access"),
        ("something new", "hidden: GitHub refused access"),
        (None, "hidden: GitHub refused access"),
    ],
)
def test_each_reason_has_its_own_words(reason: object, words: str) -> None:
    text = note(hidden({"owner": "horae-dev", "reason": reason, "count": 9}, partial=True))
    assert text.startswith(f"- github.updates: horae-dev: {words}")
    assert text.split("\n")[0].endswith(" (9)")


def test_an_unknown_owner_reads_some_items() -> None:
    text = note(hidden({"owner": None, "reason": "saml", "count": 2}))
    assert text.startswith("- github.updates: some items: hidden by SAML single sign-on")


@pytest.mark.parametrize(
    "owner", ["[[x]]", "a b", "-lead", "x" * 40, "a#tag", "a::b", "evil.example", 3]
)
def test_an_owner_that_is_not_a_login_is_not_written(owner: object) -> None:
    text = note(hidden({"owner": owner, "reason": "saml", "count": 1}))
    assert text.startswith("- github.updates: some items: hidden by SAML")
    assert str(owner) not in text.split("\n")[0]


def test_malformed_hidden_entries_are_ignored_and_a_zero_count_is_not_shown() -> None:
    text = note(hidden("x", 3, {"owner": "acme", "reason": "saml", "count": -1}))
    assert text.split("\n\n")[0] == (
        "- github.updates: acme: hidden by SAML single sign-on (authorise the token for SSO)"
    )


def test_an_empty_hidden_list_raises_no_alert() -> None:
    assert note(hidden()).startswith("_None._")


def test_without_hidden_an_older_source_keeps_the_partial_line() -> None:
    for name, line in (
        ("updates-saml", "- github.updates: GitHub hid some results (1 errors)"),
        ("ci-failures-saml", "- github.ci_failures: GitHub hid some results (3 errors)"),
    ):
        entry = recorded(name)
        assert "hidden" not in entry["data"]
        assert note(entry).split("\n\n")[0] == line
    old = {"source": "github.updates", "data": {"items": [], "partial": True}}
    assert note(old).startswith("- github.updates: GitHub hid some results\n")


def test_checks_hidden_from_this_token() -> None:
    text = note(recorded("ci-failures-checks-hidden"))
    assert " main — checks hidden from this token failing since 2026-10-29\n" in text
    some = {
        "source": "github.ci_failures",
        "data": {
            "items": [
                {"repo": "a/b", "branch": "main", "checks": ["status"], "checks_hidden": True},
                {"repo": "a/c", "branch": "main", "checks": [], "checks_hidden": False},
            ]
        },
    }
    text = note(some)
    assert "a/b main — status failing" in text  # names read are listed
    assert "a/c main — failing checks failing" in text
