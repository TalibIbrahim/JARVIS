"""
tests/test_calendar_model.py — Unit Tests for Dynamic Island Calendar Data Model.

Verifies:
  1. Calendar generation for arbitrary target dates.
  2. Leap year math (2024 leap year vs 2026 non-leap year, century rules).
  3. Week start days (Sunday-first vs Monday-first headers and column alignments).
  4. Weekend flags (Saturday & Sunday flagged, weekdays unflagged).
  5. Month boundary padding and is_current_month transitions.
  6. is_today flag accuracy.
  7. Dictionary serialization.

Strictly NO Qt imports. 100% unit-testable.
"""

import datetime
import unittest

from calendar_model import (
    CalendarDay,
    CalendarData,
    get_calendar_data,
    resolve_first_weekday_code,
    WEEKDAY_HEADERS_SUNDAY,
    WEEKDAY_HEADERS_MONDAY,
)


class TestCalendarModel(unittest.TestCase):
    """Unit tests for calendar_model functions and data classes."""

    def test_default_target_date_uses_today(self):
        """When target_date is omitted, generates data for current date."""
        today = datetime.date(2026, 10, 18)
        data = get_calendar_data(today=today)
        self.assertEqual(data.year, 2026)
        self.assertEqual(data.month_name, "OCTOBER")
        self.assertEqual(data.day, 18)
        self.assertEqual(data.weekday_name, "Sunday")

        # Today cell must be found in grid with is_today=True
        today_cells = [day for week in data.grid for day in week if day.is_today]
        self.assertEqual(len(today_cells), 1)
        self.assertEqual(today_cells[0].day_number, 18)
        self.assertTrue(today_cells[0].is_current_month)

    def test_october_2026_specific_date(self):
        """Tests calendar structure for October 2026."""
        target = datetime.date(2026, 10, 18)
        data = get_calendar_data(target_date=target, first_weekday="sunday", today=target)

        self.assertEqual(data.month_name, "OCTOBER")
        self.assertEqual(data.year, 2026)
        self.assertEqual(data.day, 18)
        self.assertEqual(data.weekday_name, "Sunday")
        self.assertEqual(data.weekday_headers, WEEKDAY_HEADERS_SUNDAY)

        # October has 31 days
        cur_month_days = [day for week in data.grid for day in week if day.is_current_month]
        self.assertEqual(len(cur_month_days), 31)

        # Grid rows must all have exactly 7 days
        for week in data.grid:
            self.assertEqual(len(week), 7)

    def test_leap_years_february_2024_vs_2026(self):
        """Leap year 2024 has 29 days in Feb; non-leap 2026 has 28."""
        # 2024 is a leap year
        leap_data = get_calendar_data(target_date=datetime.date(2024, 2, 1))
        leap_feb_days = [d for w in leap_data.grid for d in w if d.is_current_month]
        self.assertEqual(len(leap_feb_days), 29)
        self.assertEqual(leap_feb_days[-1].day_number, 29)

        # 2026 is NOT a leap year
        non_leap_data = get_calendar_data(target_date=datetime.date(2026, 2, 1))
        non_leap_feb_days = [d for w in non_leap_data.grid for d in w if d.is_current_month]
        self.assertEqual(len(non_leap_feb_days), 28)
        self.assertEqual(non_leap_feb_days[-1].day_number, 28)

        # Century leap rule: 2000 is leap, 1900 is not
        cent_leap = get_calendar_data(target_date=datetime.date(2000, 2, 1))
        self.assertEqual(len([d for w in cent_leap.grid for d in w if d.is_current_month]), 29)

        cent_non_leap = get_calendar_data(target_date=datetime.date(1900, 2, 1))
        self.assertEqual(len([d for w in cent_non_leap.grid for d in w if d.is_current_month]), 28)

    def test_week_start_sunday(self):
        """Sunday-first grid has Sunday in column 0 and Saturday in column 6."""
        data = get_calendar_data(target_date=datetime.date(2026, 10, 1), first_weekday="sunday")
        self.assertEqual(data.weekday_headers, ["S", "M", "T", "W", "T", "F", "S"])

        for week in data.grid:
            # Column 0 is Sunday -> date.weekday() == 6
            self.assertEqual(week[0].date.weekday(), 6)
            # Column 6 is Saturday -> date.weekday() == 5
            self.assertEqual(week[6].date.weekday(), 5)

    def test_week_start_monday(self):
        """Monday-first grid has Monday in column 0 and Sunday in column 6."""
        data = get_calendar_data(target_date=datetime.date(2026, 10, 1), first_weekday="monday")
        self.assertEqual(data.weekday_headers, ["M", "T", "W", "T", "F", "S", "S"])

        for week in data.grid:
            # Column 0 is Monday -> date.weekday() == 0
            self.assertEqual(week[0].date.weekday(), 0)
            # Column 6 is Sunday -> date.weekday() == 6
            self.assertEqual(week[6].date.weekday(), 6)

    def test_weekend_flags(self):
        """Saturday and Sunday must have is_weekend == True; weekdays False."""
        data = get_calendar_data(target_date=datetime.date(2026, 10, 18), first_weekday="sunday")

        for week in data.grid:
            for day in week:
                # 5 = Saturday, 6 = Sunday
                if day.date.weekday() in (5, 6):
                    self.assertTrue(day.is_weekend, f"{day.date} should be weekend")
                else:
                    self.assertFalse(day.is_weekend, f"{day.date} should not be weekend")

    def test_month_boundaries_padding(self):
        """Days outside current month have is_current_month=False."""
        # October 1, 2026 is a Thursday
        data = get_calendar_data(target_date=datetime.date(2026, 10, 1), first_weekday="sunday")
        first_week = data.grid[0]

        # Sunday through Wednesday of first week are late September (27, 28, 29, 30)
        self.assertFalse(first_week[0].is_current_month)  # Sep 27
        self.assertEqual(first_week[0].day_number, 27)
        self.assertFalse(first_week[1].is_current_month)  # Sep 28
        self.assertFalse(first_week[2].is_current_month)  # Sep 29
        self.assertFalse(first_week[3].is_current_month)  # Sep 30
        self.assertTrue(first_week[4].is_current_month)   # Oct 1
        self.assertEqual(first_week[4].day_number, 1)

    def test_to_dict_serialization(self):
        """to_dict produces JSON-compatible dictionary."""
        target = datetime.date(2026, 10, 18)
        data = get_calendar_data(target_date=target, today=target)
        d = data.to_dict()

        self.assertIsInstance(d, dict)
        self.assertEqual(d["month_name"], "OCTOBER")
        self.assertEqual(d["year"], 2026)
        self.assertEqual(d["day"], 18)
        self.assertEqual(d["weekday_name"], "Sunday")
        self.assertIsInstance(d["grid"], list)
        self.assertIsInstance(d["grid"][0][0], dict)
        self.assertIn("day_number", d["grid"][0][0])
        self.assertIn("is_weekend", d["grid"][0][0])


if __name__ == "__main__":
    unittest.main()
