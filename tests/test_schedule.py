"""AUTO_WINDOW parsing and evaluation, including windows that cross midnight."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from nobookends_sync.config import Settings, auto_cpu_threads
from nobookends_sync.schedule import Window, local_now, parse_window, window_open


def test_parse_basic():
    w = parse_window("01:00-06:00")
    assert w == Window(time(1, 0), time(6, 0))
    assert str(w) == "01:00-06:00"
    assert parse_window(" 1:30 - 6 ") == Window(time(1, 30), time(6, 0))
    assert parse_window("22:00-24:00") == Window(time(22, 0), time(0, 0))


@pytest.mark.parametrize("spec", [None, "", "  ", "always", "ALWAYS"])
def test_parse_disabled(spec):
    assert parse_window(spec) is None
    assert window_open(parse_window(spec), datetime(2026, 1, 1, 12, 0))


@pytest.mark.parametrize("spec", ["01:00", "1-2-3", "25:00-06:00", "01:60-02:00", "night", "01:00-xx"])
def test_parse_invalid(spec):
    with pytest.raises(ValueError):
        parse_window(spec)


@pytest.mark.parametrize("hhmm,inside", [
    ("00:59", False), ("01:00", True), ("03:30", True), ("05:59", True), ("06:00", False), ("12:00", False),
])
def test_same_day_window(hhmm, inside):
    w = parse_window("01:00-06:00")
    h, m = map(int, hhmm.split(":"))
    assert w.contains(time(h, m)) is inside
    assert w.contains(time(h, m, 59, 999)) is inside  # seconds do not matter


@pytest.mark.parametrize("hhmm,inside", [
    ("21:59", False), ("22:30", True), ("23:59", True), ("00:00", True), ("04:59", True), ("05:00", False),
    ("12:00", False),
])
def test_window_crossing_midnight(hhmm, inside):
    w = parse_window("22:30-05:00")
    h, m = map(int, hhmm.split(":"))
    assert w.contains(time(h, m)) is inside


def test_equal_start_and_end_is_whole_day():
    w = parse_window("03:00-03:00")
    assert all(w.contains(time(h, 0)) for h in range(24))


def test_window_open_uses_local_wall_clock():
    w = parse_window("01:00-06:00")
    ny = ZoneInfo("America/New_York")
    assert window_open(w, datetime(2026, 7, 1, 2, 0, tzinfo=ny))
    assert not window_open(w, datetime(2026, 7, 1, 7, 0, tzinfo=ny))


def test_local_now_honors_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert local_now().utcoffset().total_seconds() == 9 * 3600
    monkeypatch.setenv("TZ", "Not/AZone")
    local_now()  # falls back to the system local time instead of raising


def test_settings_window(monkeypatch):
    monkeypatch.delenv("AUTO_WINDOW", raising=False)
    assert Settings().window == Window(time(1, 0), time(6, 0))
    monkeypatch.setenv("AUTO_WINDOW", "")
    assert Settings().window is None
    monkeypatch.setenv("AUTO_WINDOW", "always")
    assert Settings().window is None
    monkeypatch.setenv("AUTO_WINDOW", "23:00-02:00")
    assert Settings().window == Window(time(23, 0), time(2, 0))


def test_cpu_threads(monkeypatch):
    monkeypatch.setenv("WHISPER_CPU_THREADS", "6")
    assert Settings().cpu_threads == 6
    monkeypatch.setenv("WHISPER_CPU_THREADS", "auto")
    assert Settings().cpu_threads == auto_cpu_threads()
    monkeypatch.setenv("WHISPER_CPU_THREADS", "0")
    assert 1 <= Settings().cpu_threads <= 16
