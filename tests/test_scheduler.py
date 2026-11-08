from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.scheduler import build_task_xml, next_occurrence, parse_schedule_time  # noqa: E402


class SchedulerTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(parse_schedule_time("09:30").hour, 9)
        self.assertEqual(parse_schedule_time("09:30").minute, 30)
        with self.assertRaises(ValueError):
            parse_schedule_time("25:00")

    def test_next_occurrence_same_day_and_next_day(self):
        tz = timezone(timedelta(hours=8))
        morning = datetime(2026, 11, 8, 9, 0, tzinfo=tz)
        next_same = next_occurrence(morning, "09:30")
        self.assertEqual(next_same.date().isoformat(), "2026-11-08")
        self.assertEqual((next_same.hour, next_same.minute), (9, 30))

        later = datetime(2026, 11, 8, 10, 0, tzinfo=tz)
        next_day = next_occurrence(later, "09:30")
        self.assertEqual(next_day.date().isoformat(), "2026-11-09")

    def test_task_xml_contains_entrypoint(self):
        settings = load_settings(project_root=ROOT)
        xml = build_task_xml(settings, "09:30")
        self.assertIn("09:30:00", xml)
        self.assertIn("run_scheduled.ps1", xml)
        self.assertIn("IgnoreNew", xml)
        self.assertIn("StartWhenAvailable", xml)


if __name__ == "__main__":
    unittest.main()
