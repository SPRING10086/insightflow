from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.utils import (  # noqa: E402
    fmt_money,
    looks_garbled,
    maybe_repair_text,
    normalize_text,
    parse_date_value,
    parse_number,
    parse_timestamp_value,
    pct_change,
)


class DateParsingTests(unittest.TestCase):
    def test_date_variants(self):
        cases = [
            "2026-11-07",
            "2026/11/7",
            "2026.11.07",
            "2026年11月7日",
            "20261107",
            "2026-11-07 09:31:00",
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_date_value(value), date(2026, 11, 7))

    def test_excel_serial(self):
        serial = (date(2026, 11, 7) - date(1899, 12, 30)).days
        self.assertEqual(parse_date_value(serial), date(2026, 11, 7))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(parse_date_value("2026-02-30"))
        self.assertIsNone(parse_date_value("not-a-date"))

    def test_timestamp_preserves_hour(self):
        parsed = parse_timestamp_value("2026-11-07 09:31:00")
        self.assertEqual(parsed.hour, 9)
        self.assertEqual(parsed.minute, 31)


class NumberParsingTests(unittest.TestCase):
    def test_units_and_separators(self):
        self.assertEqual(parse_number("¥1,234.50"), 1234.5)
        self.assertEqual(parse_number("1.2万"), 12000)
        self.assertEqual(parse_number("3.5亿"), 350000000)
        self.assertEqual(parse_number("12.5%"), 0.125)
        self.assertEqual(parse_number("(1,200)"), -1200)

    def test_missing_tokens(self):
        for value in ("", "—", "N/A", "暂无", None):
            with self.subTest(value=value):
                self.assertIsNone(parse_number(value))


class TextAndMojibakeTests(unittest.TestCase):
    def test_normalize_fullwidth(self):
        self.assertEqual(normalize_text("　ＡＢＣ　１２３　"), "ABC 123")

    def test_gbk_mojibake_repair(self):
        broken = "订单日期".encode("utf-8").decode("gbk")
        self.assertTrue(looks_garbled(broken))
        self.assertEqual(maybe_repair_text(broken), "订单日期")

    def test_normal_chinese_is_untouched(self):
        self.assertEqual(maybe_repair_text("华东区域"), "华东区域")

    def test_change_ratio(self):
        self.assertAlmostEqual(pct_change(110, 100), 10.0)
        self.assertIsNone(pct_change(1, 0))
        self.assertEqual(fmt_money(1234.5), "¥1,234.50")


if __name__ == "__main__":
    unittest.main()
