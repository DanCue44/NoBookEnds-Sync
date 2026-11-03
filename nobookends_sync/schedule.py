"""Overnight window for automatic work.

Automatic and backfill alignments (priority below user) only start or resume
inside AUTO_WINDOW, a daily local-time window such as "01:00-06:00". Windows
may cross midnight ("22:30-05:00"). An empty value or "always" disables the
window, so automatic work runs at any time. Local time follows the container's
TZ environment variable.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime

log = logging.getLogger("nbs.schedule")

DEFAULT_WINDOW = "01:00-06:00"

_HHMM = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$")


@dataclass(frozen=True)
class Window:
    start: dtime
    end: dtime

    def contains(self, t: dtime) -> bool:
        """True when local wall-clock time t is inside the window (start
        inclusive, end exclusive). start == end means the whole day."""
        t = dtime(t.hour, t.minute)
        if self.start == self.end:
            return True
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # crosses midnight

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def _parse_hhmm(s: str) -> dtime:
    m = _HHMM.match(s)
    if not m:
        raise ValueError(f"invalid time {s!r} (expected HH:MM)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h == 24 and mi == 0:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"invalid time {s!r} (expected HH:MM)")
    return dtime(h, mi)


def parse_window(spec: str | None) -> Window | None:
    """Parse "HH:MM-HH:MM". Returns None (no window, always allowed) for an
    empty value or "always". Raises ValueError for anything else."""
    if spec is None:
        return None
    s = spec.strip().lower()
    if s in ("", "always"):
        return None
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(f"invalid AUTO_WINDOW {spec!r} (expected e.g. 01:00-06:00, or always)")
    return Window(_parse_hhmm(parts[0]), _parse_hhmm(parts[1]))


def local_now() -> datetime:
    """Current local time, honoring the TZ environment variable even when the
    image has no system zoneinfo (the tzdata package provides it)."""
    tz = os.environ.get("TZ", "").strip()
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz.lstrip(":")))
        except Exception:  # noqa: BLE001
            log.debug("unknown TZ %r, using the system local time", tz)
    return datetime.now().astimezone()


def window_open(window: Window | None, now: datetime | None = None) -> bool:
    if window is None:
        return True
    now = now or local_now()
    return window.contains(now.time())
