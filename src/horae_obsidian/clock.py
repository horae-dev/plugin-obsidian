# SPDX-License-Identifier: Apache-2.0
"""The instance time zone: the core starts the plugin with the instance's `TZ`.

Times are converted with the zone's **rules** (`zoneinfo`), never with the offset of the
current moment: a snooze in July written in November is still at summer time. When `TZ`
names no IANA zone (unset, a POSIX string such as `CET-1CEST`), the process's local time
rules are used (`datetime.astimezone()`, which follows `TZ` too).
"""

import os
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def now() -> datetime:
    """The current time in the instance time zone."""
    return datetime.now().astimezone()


def zone() -> tzinfo | None:
    """The IANA zone `TZ` names (`Europe/Paris`, `:Europe/Paris`), else `None`."""
    name = os.environ.get("TZ", "").removeprefix(":")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def local(moment: datetime) -> datetime:
    """The aware `moment` in the instance time zone (its rules at that moment)."""
    rules = zone()
    return moment.astimezone(rules) if rules is not None else moment.astimezone()


def localize(naive: datetime) -> datetime:
    """The naive wall-clock time `naive`, read in the instance time zone."""
    rules = zone()
    return naive.replace(tzinfo=rules) if rules is not None else naive.astimezone()


__all__ = ["local", "localize", "now", "zone"]
