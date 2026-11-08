from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.readers import build_frame, decode_bytes, detect_delimiter, detect_header_row  # noqa: E402


class ReaderPrimitiveTests(unittest.TestCase):
    def test_header_detection_skips_title_row(self):
        rows = [
            ["2026 年 11 月销售报表"],
            ["日期", "区域", "品类", "销售额"],
            ["2026-11-07", "华东", "手机数码", "1000"],
        ]
        self.assertEqual(detect_header_row(rows, 10), 1)

    def test_delimiter_detection(self):
        text = "日期;区域;销售额\n2026-11-07;华东;1000\n2026-11-07;华南;800\n"
        delimiter, _ = detect_delimiter(text)
        self.assertEqual(delimiter, ";")

    def test_decode_gbk_without_replacement(self):
        text = "日期,区域,销售额\n2026-11-07,华东,1000\n".encode("gbk")
        decoded, encoding, _ = decode_bytes(text)
        self.assertIn("日期", decoded)
        self.assertIn(encoding, {"gb18030", "gbk"})

    def test_build_frame_drops_empty_columns(self):
        frame = build_frame(["日期", "区域", "空列"], [["2026-11-07", "华东", None], ["2026-11-08", "华南", None]])
        self.assertEqual(list(frame.columns), ["日期", "区域"])
        self.assertEqual(len(frame), 2)

    def test_single_column_sample(self):
        frame = build_frame(["备注"], [["a"], ["b"]])
        self.assertEqual(frame.shape, (2, 1))


if __name__ == "__main__":
    unittest.main()
