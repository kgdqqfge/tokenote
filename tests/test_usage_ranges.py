from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

from tokenote.credits import CreditEvent
from tokenote.db import UsageDatabase
from tokenote.models import UsageEvent
from tokenote.ui.app import TokenoteApp


def _epoch(days_ago: int, hour: int = 12) -> int:
    target = date.today() - timedelta(days=days_ago)
    return int(datetime.combine(target, dt_time(hour=hour)).timestamp())


class UsageRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = UsageDatabase(Path(self.temp.name) / 'meter.db')
        self.db.upsert_events([
            UsageEvent('today', 'antigravity', occurred_at=_epoch(0), total_tokens=100),
            UsageEvent('ten-days', 'antigravity', occurred_at=_epoch(10), total_tokens=200),
            UsageEvent('forty-days', 'qoder', occurred_at=_epoch(40), total_tokens=300),
        ])
        self.db.upsert_credit_events([
            CreditEvent('credit-today', 'qoder', occurred_at=_epoch(0), credits=1.5),
            CreditEvent('credit-ten-days', 'qoder', occurred_at=_epoch(10), credits=2.5),
        ])

    def test_daily_totals_window_keeps_zero_filled_days(self):
        week = self.db.daily_totals('antigravity', days=7)
        month = self.db.daily_totals('antigravity', days=30)
        self.assertEqual(len(week), 7)
        self.assertEqual(week[0]['day'], (date.today() - timedelta(days=6)).isoformat())
        self.assertEqual(week[-1]['day'], date.today().isoformat())
        self.assertEqual(sum(row['total'] for row in week), 100)
        self.assertEqual(sum(row['total'] for row in month), 300)

    def test_all_dates_fills_the_full_calendar_range(self):
        rows = self.db.daily_totals_all('antigravity')
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0]['day'], (date.today() - timedelta(days=10)).isoformat())
        self.assertEqual(rows[-1]['day'], date.today().isoformat())
        self.assertEqual([row['total'] for row in rows if row['total']], [200, 100])
        self.assertEqual(sum(row['total'] for row in self.db.daily_totals_all()), 600)

    def test_credit_all_dates_fills_the_full_calendar_range(self):
        rows = self.db.credit_daily_totals_all('qoder')
        self.assertEqual(len(rows), 11)
        self.assertEqual(sum(row['total'] for row in rows), 4.0)
        self.assertEqual(sum(row['credit_reports'] for row in rows), 2)

    def test_chart_since_matches_the_selected_local_day_window(self):
        class Dummy:
            chart_range_days = 7

        expected = int(datetime.combine(date.today() - timedelta(days=6), dt_time.min).timestamp())
        self.assertEqual(TokenoteApp._chart_since(Dummy()), expected)
        Dummy.chart_range_days = 30
        expected = int(datetime.combine(date.today() - timedelta(days=29), dt_time.min).timestamp())
        self.assertEqual(TokenoteApp._chart_since(Dummy()), expected)
        Dummy.chart_range_days = None
        self.assertIsNone(TokenoteApp._chart_since(Dummy()))

    def test_chart_range_text(self):
        class Dummy:
            chart_range_days = 7

        self.assertEqual(TokenoteApp._chart_range_text(Dummy()), '最近 7 天')
        Dummy.chart_range_days = 30
        self.assertEqual(TokenoteApp._chart_range_text(Dummy()), '最近 30 天')
        Dummy.chart_range_days = None
        self.assertEqual(TokenoteApp._chart_range_text(Dummy()), '所有日期')


if __name__ == '__main__':
    unittest.main()
