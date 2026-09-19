"""NUS academic-year helpers.

The NUS academic year starts in August: Semester 1 runs Aug-Dec, Semester 2 Jan-May, and
the special terms fill Jun-Jul. So 12 Mar 2026 is "AY25/26 S2" and 1 Sep 2026 is "AY26/27 S1".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

# "AY24/25", "AY 2024/25", "AY2024/2025", "AY 24-25", and bare "2024/2025" or "2024/25".
_AY_PATTERNS = [
    re.compile(r"\bAY\s?(?:20)?(\d{2})\s?[/-]\s?(?:20)?(\d{2})\b", re.I),
    re.compile(r"\b20(\d{2})\s?/\s?(?:20)?(\d{2})\b"),
]


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ay_start_year(dt: datetime) -> int:
    dt = _utc(dt)
    return dt.year if dt.month >= 8 else dt.year - 1


def semester_of(dt: datetime) -> str:
    m = _utc(dt).month
    if m >= 8:
        return "S1"
    if m <= 5:
        return "S2"
    return "ST"


def ay_label(dt: datetime, *, with_sem: bool = False) -> str:
    y = ay_start_year(dt)
    label = f"AY{y % 100:02d}/{(y + 1) % 100:02d}"
    return f"{label} {semester_of(dt)}" if with_sem else label


def ay_short(start_year: int) -> str:
    return f"{start_year % 100:02d}/{(start_year + 1) % 100:02d}"


def ay_range_label(dates: Iterable[datetime]) -> str | None:
    """'AY25/26' or 'AY23/24–25/26' for a set of dates; None when there are none."""
    years = sorted({ay_start_year(d) for d in dates})
    if not years:
        return None
    if years[0] == years[-1]:
        return f"AY{ay_short(years[0])}"
    return f"AY{ay_short(years[0])}–{ay_short(years[-1])}"


def age_years(dt: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    return max(0.0, (now - _utc(dt)).days / 365.25)


def cutoff(max_age_years: float, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now - timedelta(days=round(max_age_years * 365.25))


def infer_date_from_text(text: str, now: datetime | None = None) -> datetime | None:
    """Best guess at when an undated page was written, from the newest academic year it
    mentions ("AY24/25", "2024/2025"). Returns mid-August of that year's start, or None.
    Years in the future or before 2000 are ignored, so a syllabus mentioning "2030/31" or
    a stray "1999/2000" does not skew it."""
    now = now or datetime.now(UTC)
    current = ay_start_year(now)
    best: int | None = None
    for pat in _AY_PATTERNS:
        for a, b in pat.findall(text):
            start, end = int(a), int(b)
            if (end - start) % 100 != 1:  # must be consecutive years, e.g. 24/25
                continue
            year = 2000 + start
            if 2000 <= year <= current and (best is None or year > best):
                best = year
    return datetime(best, 8, 15, tzinfo=UTC) if best is not None else None


def ay_windows(max_age_years: float, now: datetime | None = None) -> list[tuple[str, str | None]]:
    """Academic-year date windows, newest first, covering the last ``max_age_years``:
    [("2026-08-01", None), ("2025-08-01", "2026-08-01"), ...]. The oldest window starts at
    the cutoff date rather than a year boundary."""
    now = now or datetime.now(UTC)
    oldest = cutoff(max_age_years, now)
    windows: list[tuple[str, str | None]] = []
    start = ay_start_year(now)
    before: str | None = None
    while True:
        after_dt = datetime(start, 8, 1, tzinfo=UTC)
        if after_dt <= oldest:
            windows.append((oldest.date().isoformat(), before))
            break
        windows.append((after_dt.date().isoformat(), before))
        before = after_dt.date().isoformat()
        start -= 1
    return windows
