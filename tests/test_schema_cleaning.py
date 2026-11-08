from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.cleaning import clean_frame  # noqa: E402
from insightflow.schema import detect_schema  # noqa: E402


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame(
            {
                "订单日期": ["2026-11-07", "2026-11-07", "2026-11-06"],
                "区域": ["华东", "华南", "华东"],
                "品类": ["手机数码", "家用电器", "手机数码"],
                "销售额(万元)": ["1.2", "0.8", "1.0"],
                "订单量": ["2", "1", "2"],
                "成本": ["0.7", "0.5", "0.6"],
            }
        )
        self.schema = detect_schema(self.frame)

    def test_semantic_roles(self):
        kinds = {field.canonical: field.kind for field in self.schema.fields.values()}
        self.assertEqual(kinds["date"], "date")
        self.assertEqual(kinds["region"], "dimension")
        self.assertEqual(kinds["revenue"], "metric")
        self.assertEqual(kinds["orders"], "metric")

    def test_unit_multiplier(self):
        revenue = self.schema.fields["revenue"]
        self.assertEqual(revenue.unit, "元")
        self.assertEqual(revenue.multiplier, 10000)

    def test_no_date_is_flagged(self):
        schema = detect_schema(pd.DataFrame({"商品": ["A", "B"], "单价": [1, 2]}))
        self.assertTrue(any(issue.code == "no_date_column" for issue in schema.issues))

    def test_english_dirty_sales_headers(self):
        schema = detect_schema(
            pd.DataFrame(
                {
                    "Transaction Date": ["2023-11-08", "2023-11-07"],
                    "Item": ["Coffee", "Cake"],
                    "Quantity": [2, 4],
                    "Total Spent": [4.0, 12.0],
                    "Location": ["Takeaway", "In-store"],
                }
            )
        )
        self.assertEqual(schema.fields["revenue"].original, "Total Spent")
        self.assertEqual(schema.fields["orders"].original, "Quantity")
        self.assertEqual(schema.fields["product"].original, "Item")


class CleaningTests(unittest.TestCase):
    def setUp(self):
        frame = pd.DataFrame(
            {
                "__date": [date(2026, 11, 7)] * 6,
                "__hour": [9, 10, 11, 12, 13, 14],
                "__timestamp": [None] * 6,
                "__period": ["target"] * 6,
                "__record": [1] * 6,
                "__source_file": ["a.csv"] * 6,
                "__source_sheet": ["CSV"] * 6,
                "__source_row": [2, 3, 4, 5, 6, 7],
                "revenue": [100.0, 110.0, 120.0, None, 130.0, 10000.0],
                "orders": [1, 1, 2, 1, 1, 9],
                "region": ["华东", "华东", "华南", "", "华北", "华东"],
                "category": ["手机", "手机", "家电", "家电", "服饰", "手机"],
                "order_id": ["1", "2", "3", "4", "5", "6"],
            }
        )
        # Exact duplicate row to exercise de-duplication.
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        schema = detect_schema(frame.rename(columns={}))
        self.fields = schema.fields
        self.frame = frame

    def test_cleaning_removes_duplicate_fills_missing_and_flags_outlier(self):
        cleaned, report = clean_frame(self.frame, self.fields, date(2026, 11, 7), 1.5, 0)
        self.assertEqual(report.duplicates_removed, 1)
        self.assertEqual(len(cleaned), 6)
        self.assertFalse(cleaned["revenue"].isna().any())
        self.assertIn("revenue", report.missing_filled)
        self.assertGreater(report.outlier_counts.get("revenue", 0), 0)
        self.assertIn("completeness", report.quality_components)
        self.assertGreater(report.quality_score, 0)


if __name__ == "__main__":
    unittest.main()
