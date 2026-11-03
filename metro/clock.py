"""US market calendar (NYSE rules), New York and Israel clocks.

Holidays are computed from the published NYSE rules. Rare one-off closures
(national days of mourning, storms) are not predictable; add them to EXTRA_CLOSED.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
IL = ZoneInfo("Asia/Jerusalem")
EXTRA_CLOSED: set[date] = set()

OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)
PRE_START, POST_END = time(4, 0), time(20, 0)


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def holidays(y: int) -> set[date]:
    out = {
        _nth_weekday(y, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(y, 2, 0, 3),  # Washington's Birthday
        _easter(y) - timedelta(days=2),  # Good Friday
        _last_weekday(y, 5, 0),  # Memorial Day
        _nth_weekday(y, 9, 0, 1),  # Labor Day
        _nth_weekday(y, 11, 3, 4),  # Thanksgiving
    }
    ny = date(y, 1, 1)
    if ny.weekday() != 5:  # NYSE does not close Friday Dec 31 for a Saturday New Year
        out.add(_observed(ny))
    for m, d in ((6, 19), (7, 4), (12, 25)):
        out.add(_observed(date(y, m, d)))
    return out | {d for d in EXTRA_CLOSED if d.year == y}


def early_closes(y: int) -> set[date]:
    out = {_nth_weekday(y, 11, 3, 4) + timedelta(days=1)}  # day after Thanksgiving
    for d in (date(y, 7, 3), date(y, 12, 24)):
        if d.weekday() < 5 and d not in holidays(y):
            out.add(d)
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def add_trading_days(d: date, n: int) -> date:
    for _ in range(abs(n)):
        d = next_trading_day(d) if n > 0 else prev_trading_day(d)
    return d


def trading_days_between(a: date, b: date) -> int:
    """Trading days in (a, b]."""
    n, d = 0, a
    while d < b:
        d = next_trading_day(d)
        if d <= b:
            n += 1
    return n


def now_ny() -> datetime:
    return datetime.now(NY)


def session_state(at: datetime | None = None) -> str:
    """open | pre | post | closed."""
    at = (at or now_ny()).astimezone(NY)
    d, t = at.date(), at.time()
    if not is_trading_day(d):
        return "closed"
    close = EARLY_CLOSE if d in early_closes(d.year) else CLOSE
    if OPEN <= t < close:
        return "open"
    if PRE_START <= t < OPEN:
        return "pre"
    if close <= t < POST_END:
        return "post"
    return "closed"


def last_session(at: datetime | None = None) -> date:
    """The most recent session that has started (today once 09:30 NY has passed)."""
    at = (at or now_ny()).astimezone(NY)
    d = at.date()
    if is_trading_day(d) and at.time() >= OPEN:
        return d
    return prev_trading_day(d)


def session_progress(at: datetime | None = None) -> float:
    """Expected share of the day's volume traded so far (U-shaped intraday curve)."""
    at = (at or now_ny()).astimezone(NY)
    if session_state(at) != "open":
        return 1.0
    close = EARLY_CLOSE if at.date() in early_closes(at.year) else CLOSE
    total = (close.hour * 60 + close.minute) - (OPEN.hour * 60 + OPEN.minute)
    m = (at.hour * 60 + at.minute) - (OPEN.hour * 60 + OPEN.minute)
    x = max(0.0, min(1.0, m / total))
    # cumulative of a U-shaped density: heavy open, quiet midday, heavy close
    cum = 0.42 * x + 0.29 * (1 - (1 - x) ** 3) + 0.29 * x ** 3
    return max(0.05, min(1.0, cum))


def fmt_hhmm(at: datetime, tz: ZoneInfo) -> str:
    return at.astimezone(tz).strftime("%H:%M")
