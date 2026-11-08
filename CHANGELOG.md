# Changelog

## 1.0.1 — 2026-11-08

### P0 阻断级修复

- **P0-1 Excel 业务行截断**
  - 截断判断从“物理行数（含表头）”改为“表头之后的数据行数”。
  - 恰好等于 `max_rows_per_file` 的 Excel 数据不再少 1 行；CSV / Excel 行为一致。
  - 回归测试：`test_excel_does_not_lose_one_row_at_the_business_row_limit`。

- **P0-2 源文件行号**
  - 读取层保存物理行号：CSV 使用 `csv.reader.line_num`，Excel 使用 Sheet 行。
  - 行号通过隐藏列 `__source_row` 穿过日期筛选、去重、排序；Schema 明确跳过 `__` 元数据列。
  - 回归测试：CSV / Excel 的 `test_source_row_uses_physical_*`。

### P1 严重级修复

- **P1-3 别名误判**
  - `毛利率 / 利润率 / 占比 / 平均 / 非销售额` 等不可相加表头不再映射到 revenue/profit 并参与 SUM。
  - 回归测试：`test_rate_and_negative_headers_are_not_summed_as_amount_metrics`。

- **P1-4 日月歧义**
  - `03/04/2026` 这类日/月都 ≤12 的值默认按“日/月/年”解析，显式 `15/04` 或 `04/15` 仍可判定。
  - 文档建议跨区域数据使用 `YYYY-MM-DD`。
  - 回归测试：`test_ambiguous_dates_default_to_day_first`。

- **P1-5 `sheets_skipped`**
  - `TableData.sheet_total` 记录工作簿 Sheet 总数，`FileReport.sheets_skipped = total - read`，替换原来的 `len(x)-len(x)` 死代码。
  - 回归测试：`test_sheets_skipped_counts_empty_sheets`。

- **P1-6 daemon 补跑 / 失败重试**
  - 触发窗口从 35 秒改为配置的 `grace_minutes`（默认 180 分钟），休眠恢复后可补跑。
  - 失败不再写入 `last_triggered_date`；5 分钟后在宽限期内重试。
  - 回归测试：`test_trigger_is_due_within_grace_window_not_35_seconds`、`test_failed_trigger_is_not_marked_as_completed_and_can_retry`。

### P2 工程改进（本次已处理）

- `config/config.json` 加入 `.gitignore`，保留 `config.example.json` 作为模板。
- 新增 `requirements.lock.txt`，固定验证过的依赖版本。
- `run_scheduled.ps1` 不再用 `*> $null` 吞掉排障输出；完整 UTF-8 输出写入 `logs/scheduler_console.log`。
- 锁过期时间从 4 小时调整为 150 分钟，与 Windows 任务 2 小时超时对齐。
- 新增 GitHub Actions `tests.yml`，在 Windows + Python 3.10 上跑 37 个测试。

### P2 仍待处理

- 中位数填补会进入 SUM 总量（需要“有效值总量 / 填补后总量”双轨口径）。
- 混合单位（表头单位 + 单元格单位）仍可能产生口径歧义。
- 尚无覆盖率和独立对账脚本。

### 验证

- `python -m unittest discover -s tests`：**37 tests OK**。
- 新增 `tests/test_defects.py`，每个缺陷先有失败测试再修复。
