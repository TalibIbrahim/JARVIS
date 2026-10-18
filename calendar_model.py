"""
calendar_model.py — Dynamic Island Calendar Data Model.

Pure Python logic model providing calendar month grid generation, week headers,
leap year calculations, weekend detection, and day metadata.
Strictly NO Qt imports. 100% unit-testable without network dependencies.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CalendarDay:
    """Represents a single day cell in a calendar month grid."""
    day_number: int
    is_current_month: bool
    is_today: bool
    is_weekend: bool
    date: Optional[datetime.date] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day_number": self.day_number,
            "is_current_month": self.is_current_month,
            "is_today": self.is_today,
            "is_weekend": self.is_weekend,
            "date": self.date.isoformat() if self.date else None,
        }


@dataclass
class CalendarData:
    """Complete calendar representation for a given month and selected date."""
    month_name: str
    year: int
    day: int
    weekday_name: str
    weekday_headers: List[str]
    grid: List[List[CalendarDay]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "month_name": self.month_name,
            "year": self.year,
            "day": self.day,
            "weekday_name": self.weekday_name,
            "weekday_headers": self.weekday_headers,
            "grid": [[d.to_dict() for d in week] for week in self.grid],
        }


# Weekday headers based on starting day
WEEKDAY_HEADERS_SUNDAY = ["S", "M", "T", "W", "T", "F", "S"]
WEEKDAY_HEADERS_MONDAY = ["M", "T", "W", "T", "F", "S", "S"]


def resolve_first_weekday_code(first_weekday: str = "locale") -> int:
    """
    Resolves week start day to Python calendar constant:
    - calendar.SUNDAY (6)
    - calendar.MONDAY (0)
    """
    mode = str(first_weekday).strip().lower()
    if mode == "sunday":
        return calendar.SUNDAY
    if mode == "monday":
        return calendar.MONDAY

    # 'locale': check system locale if available; default to Sunday (standard US / desktop island)
    try:
        import locale
        loc = locale.getlocale()[0]
        if loc:
            loc_lower = loc.lower()
            # ISO 8601 European locales start on Monday
            if any(loc_lower.startswith(prefix) for prefix in ("en_gb", "de", "fr", "es", "it", "nl", "ru", "sv", "pl")):
                return calendar.MONDAY
    except Exception:
        pass

    return calendar.SUNDAY


def get_calendar_data(
    target_date: Optional[datetime.date] = None,
    first_weekday: str = "locale",
    today: Optional[datetime.date] = None,
) -> CalendarData:
    """
    Generates CalendarData containing the full month grid, headers, and active day indicators.

    Args:
        target_date: Date to display month and year for (defaults to today).
        first_weekday: "locale", "sunday", or "monday".
        today: Reference date considered 'today' (defaults to system date).

    Returns:
        CalendarData with month_name, year, day, weekday_name, weekday_headers, grid.
    """
    if today is None:
        today = datetime.date.today()

    if target_date is None:
        target_date = today

    fw_code = resolve_first_weekday_code(first_weekday)
    if fw_code == calendar.SUNDAY:
        weekday_headers = list(WEEKDAY_HEADERS_SUNDAY)
    else:
        weekday_headers = list(WEEKDAY_HEADERS_MONDAY)

    cal = calendar.Calendar(firstweekday=fw_code)
    raw_weeks = cal.monthdatescalendar(target_date.year, target_date.month)

    grid: List[List[CalendarDay]] = []
    for week in raw_weeks:
        week_days: List[CalendarDay] = []
        for dt in week:
            is_cur_month = (dt.year == target_date.year and dt.month == target_date.month)
            is_tod = (dt == today)
            is_wknd = (dt.weekday() in (5, 6))  # 5 is Saturday, 6 is Sunday
            week_days.append(
                CalendarDay(
                    day_number=dt.day,
                    is_current_month=is_cur_month,
                    is_today=is_tod,
                    is_weekend=is_wknd,
                    date=dt,
                )
            )
        grid.append(week_days)

    month_name = target_date.strftime("%B").upper()
    weekday_name = target_date.strftime("%A")

    return CalendarData(
        month_name=month_name,
        year=target_date.year,
        day=target_date.day,
        weekday_name=weekday_name,
        weekday_headers=weekday_headers,
        grid=grid,
    )
