#!/usr/bin/env python3
"""Generate a deterministic, deliberately imperfect demo dataset.

The dataset is synthetic and clearly labelled as such.  It exercises the same
compatibility paths as a real export: multiple files, Excel/CSV, GBK, BOM,
mojibake headers, a cover sheet, empty files, a corrupt workbook, missing
values, duplicates and outliers.

Usage:
    python scripts/generate_sample_data.py --end-date 2026-11-07
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

REGIONS = ["华东", "华南", "华北", "西南"]
REGION_FACTOR = {"华东": 1.00, "华南": 0.78, "华北": 0.62, "西南": 0.45}
CATEGORIES = ["手机数码", "家用电器", "服饰鞋包", "食品饮料"]
CATEGORY_BASE = {"手机数码": 4200, "家用电器": 2800, "服饰鞋包": 680, "食品饮料": 180}
CHANNELS = ["线上", "线下"]
CHANNEL_FACTOR = {"线上": 1.00, "线下": 0.86}
STORES = {
    "华东": ["上海静安旗舰店", "杭州湖滨店"],
    "华南": ["深圳福田店", "广州天河店"],
    "华北": ["北京朝阳店", "天津和平店"],
    "西南": ["成都春熙店", "重庆解放碑店"],
}
HEADER = ["订单编号", "订单日期", "订单时间", "区域", "渠道", "品类", "门店", "销售额", "订单量", "客户数", "新增客户数", "成本", "到店客流"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", default="2026-11-07", help="target day (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "examples" / "raw"))
    args = parser.parse_args()
    end_date = date.fromisoformat(args.end_date)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    _clear_generated(output)
    random.seed(20261107)

    detail_rows = _build_excel_rows(end_date)
    _write_workbook(output / "sales_detail.xlsx", detail_rows, end_date)
    _write_store_csv(output / "store_orders.csv", end_date)
    _write_gbk_csv(output / "legacy_gbk.csv", end_date)
    _write_mojibake_csv(output / "messy_headers.csv", end_date)
    (output / "empty.csv").write_bytes(b"")
    (output / "broken.xlsx").write_text("this is not an excel workbook", encoding="utf-8")
    _write_no_date_csv(output / "no_date.csv")
    (output / "readme.txt").write_text("This file is intentionally ignored by the pipeline.\n", encoding="utf-8")
    print(f"sample data written to {output}")
    print(f"detail rows: {len(detail_rows)}; target date: {end_date.isoformat()}")


def _clear_generated(output: Path) -> None:
    for name in (
        "sales_detail.xlsx",
        "store_orders.csv",
        "legacy_gbk.csv",
        "messy_headers.csv",
        "empty.csv",
        "broken.xlsx",
        "no_date.csv",
        "readme.txt",
    ):
        path = output / name
        if path.exists():
            path.unlink()


def _build_excel_rows(end_date: date) -> list[list[object]]:
    rows: list[list[object]] = []
    order_seq = 1
    for day_offset in range(4, -1, -1):
        day = end_date - timedelta(days=day_offset)
        for region in REGIONS:
            for category in CATEGORIES:
                for channel in CHANNELS:
                    for repeat in range(2):
                        orders = random.randint(1, 5)
                        quantity_factor = 0.75 + random.random() * 0.55
                        revenue = (
                            CATEGORY_BASE[category]
                            * REGION_FACTOR[region]
                            * CHANNEL_FACTOR[channel]
                            * quantity_factor
                            * orders
                        )
                        if day == end_date and order_seq == 3:
                            revenue *= 8.5  # deliberate IQR outlier
                        cost = revenue * (0.58 + random.random() * 0.16)
                        customers = max(1, int(orders * (0.55 + random.random() * 0.3)))
                        new_customers = max(0, int(customers * (0.04 + random.random() * 0.10)))
                        store = random.choice(STORES[region])
                        hour = random.randint(8, 22)
                        minute = random.randint(0, 59)
                        order_id = f"SO-{day.strftime('%Y%m%d')}-{order_seq:05d}"
                        rows.append(
                            [
                                order_id,
                                day.isoformat(),
                                f"{hour:02d}:{minute:02d}",
                                region,
                                channel,
                                category,
                                store,
                                round(revenue, 2),
                                orders,
                                customers,
                                new_customers,
                                round(cost, 2),
                                random.randint(18, 160),
                            ]
                        )
                        order_seq += 1
    # Missing values, a duplicate and a couple of edge cases.
    if rows:
        rows[4][7] = None
        rows[9][11] = None
        rows[14][3] = None
        rows.append(list(rows[-1]))  # exact duplicate of a target-day row, including order id
    return rows


def _write_workbook(path: Path, rows: list[list[object]], end_date: date) -> None:
    wb = Workbook()
    cover = wb.active
    cover.title = "说明"
    cover["A1"] = "InsightFlow 演示数据"
    cover["A1"].font = Font(size=18, bold=True, color="FFFFFF")
    cover["A1"].fill = PatternFill("solid", fgColor="0F172A")
    cover["A1"].alignment = Alignment(vertical="center")
    cover.merge_cells("A1:F3")
    cover["A5"] = "用途"
    cover["B5"] = "演示每日数据清洗、分析与离线报告；为合成数据，不代表真实业务。"
    cover["A6"] = "目标日期"
    cover["B6"] = end_date.isoformat()
    cover["A7"] = "数据特征"
    cover["B7"] = "多文件、多编码、缺失、重复、异常值、乱码表头、空文件与损坏文件。"
    cover.column_dimensions["A"].width = 14
    cover.column_dimensions["B"].width = 90

    sheet = wb.create_sheet("销售明细")
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    header_fill = PatternFill("solid", fgColor="1D4ED8")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    widths = [20, 12, 10, 10, 8, 12, 18, 13, 9, 9, 11, 13, 10]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:M{sheet.max_row}"

    # A second sheet with no row-level date tests the "cover sheet" path.
    summary = wb.create_sheet("口径说明")
    summary["A1"] = "字段"
    summary["B1"] = "说明"
    summary.append(["销售额", "含税销售额，单位：元"])
    summary.append(["订单量", "成交订单笔数"])
    summary.append(["到店客流", "门店当日到店客流（演示辅助指标）"])
    summary.column_dimensions["A"].width = 18
    summary.column_dimensions["B"].width = 60

    wb.save(path)


def _base_row(day: date, seq: int, region: str, category: str, channel: str, source_tag: str) -> list[object]:
    orders = random.randint(1, 4)
    revenue = CATEGORY_BASE[category] * REGION_FACTOR[region] * CHANNEL_FACTOR[channel] * orders * (0.8 + random.random() * 0.5)
    cost = revenue * (0.60 + random.random() * 0.15)
    customers = max(1, int(orders * (0.5 + random.random() * 0.4)))
    return [
        f"{source_tag}-{day.strftime('%Y%m%d')}-{seq:04d}",
        day.isoformat(),
        f"{random.randint(8, 22):02d}:{random.randint(0, 59):02d}",
        region,
        channel,
        category,
        random.choice(STORES[region]),
        round(revenue, 2),
        orders,
        customers,
        max(0, int(customers * 0.08)),
        round(cost, 2),
        random.randint(18, 160),
    ]


def _write_store_csv(path: Path, end_date: date) -> None:
    rows: list[list[object]] = []
    for day in (end_date - timedelta(days=1), end_date):
        for index, region in enumerate(REGIONS):
            for repeat in range(3):
                rows.append(
                    _base_row(
                        day,
                        index * 10 + repeat + 1,
                        region,
                        CATEGORIES[(index + repeat) % len(CATEGORIES)],
                        "线下",
                        "STORE",
                    )
                )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)


def _write_gbk_csv(path: Path, end_date: date) -> None:
    rows: list[list[object]] = []
    for day in (end_date - timedelta(days=1), end_date):
        for index in range(14):
            region = REGIONS[index % len(REGIONS)]
            rows.append(
                _base_row(
                    day,
                    index + 1,
                    region,
                    CATEGORIES[(index * 2) % len(CATEGORIES)],
                    CHANNELS[index % 2],
                    "LEGACY",
                )
            )
    with path.open("w", encoding="gbk", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)


def _write_mojibake_csv(path: Path, end_date: date) -> None:
    # Simulate a UTF-8 header that was once decoded as GBK.  These particular
    # labels round-trip without replacement characters, which keeps the test
    # fixture deterministic while still exercising the repair path.
    def mojibake(text: str) -> str:
        return text.encode("utf-8").decode("gbk")

    headers = [mojibake("订单编号"), mojibake("订单日期"), mojibake("区域"), mojibake("品类"),
               mojibake("渠道"), mojibake("营收"), mojibake("订单数量"), mojibake("成本"),
               mojibake("客户数量")]
    rows: list[list[object]] = []
    for day in (end_date - timedelta(days=1), end_date):
        for index in range(10):
            region = REGIONS[index % len(REGIONS)]
            category = CATEGORIES[(index + 1) % len(CATEGORIES)]
            revenue = CATEGORY_BASE[category] * REGION_FACTOR[region] * (0.8 + random.random() * 0.4)
            rows.append(
                [
                    f"MESSY-{day.strftime('%Y%m%d')}-{index + 1:03d}",
                    day.isoformat(),
                    region,
                    category,
                    CHANNELS[index % 2],
                    round(revenue, 2),
                    random.randint(1, 4),
                    round(revenue * 0.66, 2),
                    max(1, int(revenue / 1800)),
                ]
            )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def _write_no_date_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["商品编码", "商品名称", "单价"])
        writer.writerow(["P001", "演示商品 A", 99.0])
        writer.writerow(["P002", "演示商品 B", 129.0])


if __name__ == "__main__":
    main()
