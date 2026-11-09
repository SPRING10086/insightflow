# Changelog

## 1.0.5-dev — LLM 阶段 2：分析计划双轨

- 新增 `planning.py`（`PlanProvider` 契约、计划产物、字段闭环校验、双轨 diff、落盘）。
- 新增 `llm/plan_contracts.py` + `llm/plan_adapter.py` + `prompts/plan_v1.md`：LLM 计划生成，
  结构/空数据槽/业务数字校验，字段引用闭环（幻觉字段、text/id 当指标、metric 当维度一律拒绝）。
- `pipeline.execute(..., plan_provider=None)` 写 `plan_rule.json` / `plan_llm.json` / `plan_diff.json`；
  `run --plan {none,llm}`；异常逐级降级到规则计划。
- 3 个验收样本 + 2 个真实工商小表（2018/2019 上海前 300 行）双轨对照完成；
  真实 2018 样本语义轨因模型 JSON 不闭合降级（如实记录）。
- 测试 202 项全绿。详见 [docs/llm-stage2-notes.md](docs/llm-stage2-notes.md)。

## 1.0.4-dev — LLM 阶段 1：语义 Schema 双轨接入

- 新增 `semantic.py`（中立 `SemanticProvider` 契约 + 版本化产物 + 差异计算）、
  `semantic_assembly.py`（唯一允许引用 LLM 的装配点）、`llm/adapter.py`（实现 provider）。
- `pipeline.execute(..., semantic_provider=None)`：旁路双轨，落盘
  `semantic_rule.json` / `semantic_schema.json` / `semantic_diff.json`；默认 None 时行为与产物不变。
- `run --semantic {none,llm}`；配置缺失/请求失败/契约失败/预算熔断均逐表降级到规则语义。
- 提示词升级 `analysis_plan_v3`（relations、anomalies、严格字段覆盖、基数判定规则）；
  载荷新增 `non_empty/unique_count/unique_ratio`（只回传计数，不回传明细）；
  适配器增加覆盖校验 + 一次重试。
- 成本：单表 5 元 + 整批 20 元护栏。
- 一致率实测（golden 经审查方确认）：3 样本 **20/20 = 100%**，date/metric/id 零错误，
  异常召回 100%，单次评估成本 0.0464 元；03 的高基数文本误判已修正（8/10 → 10/10）。
  原始结果见 [docs/llm-semantics-eval.json](docs/llm-semantics-eval.json)。

## 1.0.3-dev — LLM PoC（未接入主流程）

- 新增隔离包 `src/insightflow/llm/`：载荷最小化与脱敏、版本化输出契约、标准库 API 客户端
  （重试/超时/成本熔断）、评估辅助与 CLI（`python -m insightflow.llm payload|plan`）。
- 核心与 GUI 不依赖该包，且有测试断言（核心禁止 `import insightflow.llm`；
  `llm/client.py` 是唯一允许使用 `urllib` 的文件）。
- 成本护栏：4.5 元预警、5.0 元硬停；模型标识与单价全部外置（代码不内置默认值）。
- `.gitignore` 增补 `.env`/`.env.*`（保留 `.env.example`）。
- 评估现状见 [docs/llm-poc-report.md](docs/llm-poc-report.md)：架构与安全边界已机械验证，
  真实计费/准确率待具备 API Key 的机器补测。

## 1.0.2 — 2026-11-09

### P2 商业级门槛（部分闭环）

- **P2-1 中位数填补双轨口径**
  - `CleaningReport.imputed_sums` 记录每列被填补值的金额/数量总和；`KPI` 新增
    `valid_value / valid_formatted / imputed_count`。
  - 求和型 KPI 同时给出「含填补总量」与「有效值（剔除填补）」；报告 KPI 卡片新增一行
    「有效值 … （含填补 N 个）」，比率型指标（AVG）不受影响。
  - 回归测试：`test_imputed_sum_and_valid_value_reach_the_report`、
    `test_clean_data_has_no_valid_line`。

- **P2-2 混合单位逐值换算**
  - 表头单位（如「销售额(万元)」）现在只作用于**不带单位**的单元格；带内联单位的值
    （`1.2万`、`3k`、`5w`）按自身单位换算，不再出现"整列一刀切"导致的量级错误。
  - `_INLINE_UNIT` 补齐 `百`、`w/W`，与 `parse_number` 对齐。
  - 新增 `mixed_unit` 质量告警：同列既带单位又不带单位时提示人工核对。
  - 回归测试：`test_header_and_inline_units_are_scaled_per_value`、
    `test_column_without_inline_units_uses_header_multiplier`。

- **P2-3 对账与覆盖率（已闭环）**
  - 新增 `scripts/reconcile.py`：独立于 pipeline，从产物重算行数守恒、指标合计、
    血缘行号、填补标记一致性；不一致退出码 1（真实运行 11 项全通过）。
  - 新增 `scripts/coverage_gate.py`：零依赖（`sys.settrace` + AST）核心行覆盖率门槛；
    修正了"import 早于 tracer 启动"的统计口径缺陷，并新增 cli / scheduler /
    discovery / utils 的错误分支测试。
  - **核心行覆盖率 85.2%，达到 85% 门槛**；CI 已按 85% 强制。

### 验证

- `python -m unittest discover -s tests`：**139 tests OK**。
- `python scripts/reconcile.py output/<日期>`：真实运行产物对账 11 项通过。
- `python scripts/coverage_gate.py --threshold 85`：核心行覆盖率 85.2%，通过。

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

- **P1-3 别名误判 / 率字段**
  - `毛利率 / 利润率 / 占比 / 平均 / 非销售额` 等不可相加表头不再映射到 revenue/profit 并参与 SUM。
  - 率类字段使用 `AVG` 聚合；KPI、分时、分维度、日环比均按均值，占比/帕累托在率作为主指标时禁用。
  - 回归测试：`test_rate_and_negative_headers_are_not_summed_as_amount_metrics`、`test_rate_metric_uses_average_and_skips_share`。

- **P1-4 日月歧义**
  - `03/04/2026` 这类日/月都 ≤12 的值默认按“月/日/年”解析（2026-03-04），显式 `15/04` 或 `04/15` 仍可判定。
  - 新增 `date_order` 配置（`month_first` / `day_first` / `error`），检测到歧义值会在数据质量区写 warning。
  - 即使错误假设导致目标日命中 0 行，`date_order_ambiguous` 也保留在运行日志/质量提示中。
  - 文档建议跨区域数据使用 `YYYY-MM-DD`；欧式日/月数据设置 `day_first`。
  - 回归测试：`test_ambiguous_dates_default_to_month_first`、`test_ambiguous_dates_emit_warning`、`test_date_order_warning_survives_no_target_rows`。

- **P1-5 `sheets_skipped`**
  - 读取函数返回工作簿 `total_sheets`，`FileReport.sheets_skipped = total - read`，替换原来的 `len(x)-len(x)` 死代码；全部 Sheet 失败时仍能正确统计。
  - 回归测试：`test_sheets_skipped_counts_empty_sheets`、`test_all_sheets_empty_still_reports_two_skipped`。

- **P1-6 daemon 补跑 / 失败重试**
  - 触发窗口从 35 秒改为配置的 `grace_minutes`（默认 180 分钟），休眠恢复后可补跑。
  - 失败不再写入 `last_triggered_date`；5 分钟后在宽限期内重试。
  - 回归测试：`test_trigger_is_due_within_grace_window_not_35_seconds`、`test_failed_trigger_is_not_marked_as_completed_and_can_retry`。

### P2 工程改进（本次已处理）

- `config/config.json` 加入 `.gitignore`，保留 `config.example.json` 作为模板。
- 新增 `requirements.lock.txt`，固定验证过的依赖版本。
- `run_scheduled.ps1` 不再用 `*> $null` 吞掉排障输出；完整 UTF-8 输出写入 `logs/scheduler_console.log`。
- 锁过期时间从 4 小时调整为 150 分钟，与 Windows 任务 2 小时超时对齐。
- 新增 GitHub Actions `tests.yml`，在 Windows + Python 3.10 上跑 41 个测试。

### P2 仍待处理

- 中位数填补会进入 SUM 总量（需要“有效值总量 / 填补后总量”双轨口径）。
- 混合单位（表头单位 + 单元格单位）仍可能产生口径歧义。
- 尚无覆盖率和独立对账脚本。

### 验证

- `python -m unittest discover -s tests`：**41 tests OK**。
- 新增 `tests/test_defects.py`，每个缺陷先有失败测试再修复。
