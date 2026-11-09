# P2 商业级门槛攻坚记录

> 目标：把系统从「有条件放行」推进到「无条件商业级」。
> 结论先行：**P2-1、P2-2 已闭环；P2-3 的对账脚本已闭环，覆盖率门槛机制已上线但 85% 目标未达（当前 60.3%）。因此系统仍为「有条件放行」。**

---

## 1. P2-1 中位数填补双轨口径 —— 已闭环

**问题**：缺失用中位数填补后直接进入 SUM，总量里混着估算值，报告无法区分。

**做法**：

1. `cleaning.py` 在填补时累计 `imputed_sums[列] = 填补值 × 个数`，写入 `CleaningReport`；
2. `KPI` 增加 `valid_value / valid_formatted / imputed_count`；
3. `analysis.py` 只对**求和型**指标挂双轨（比率型 AVG 不受中位数填补影响，不做假动作）；
4. 报告 KPI 卡片在总量下方追加一行「有效值 X（含填补 N 个）」，并保持原有的"含填补"总量。

**证据**：

```text
quality.missing_filled = {"revenue": 1}
quality.imputed_sums   = {"revenue": 200.0}     # median(100,300)=200
kpi.revenue: total=600  valid=400  imputed_count=1
HTML 含「有效值」「含填补 1 个」
```

测试：`test_imputed_sum_and_valid_value_reach_the_report`、`test_clean_data_has_no_valid_line`。

## 2. P2-2 混合单位逐值换算 —— 已闭环

**问题**：原实现用前 50 行抽样决定整列是否乘表头系数；只要出现一个 `1.2万`，整列都不乘，
裸值 `12000`（万元）就会被少乘 1e4，量级错 4 个数量级。

**做法**：

1. `_metric_value` 改为**逐值判断**：带内联单位（`1.2万`、`3k`、`5w`）按自身单位换算；
   裸值才继承表头系数；
2. `_INLINE_UNIT` 补齐 `百`、`w/W`，与 `parse_number` 的识别范围对齐；
3. 新增 `mixed_unit` 告警：同列既有带单位又有裸值时提示"已按逐值换算，请核对源数据"；
4. 预览（CLI scan / GUI 只预览）同样带上该告警。

**证据**（表头 `销售额(万元)`，值 `1.2万 / 12000 / 3`）：

```text
cleaned revenue = 12,000 元 / 120,000,000 元 / 30,000 元   （逐值正确）
warnings 含 1 条 mixed_unit
无内联单位的列：1、2（万元）→ 10,000 / 20,000 元，且无告警
```

测试：`test_header_and_inline_units_are_scaled_per_value`、
`test_column_without_inline_units_uses_header_multiplier`。

## 3. P2-3 对账与覆盖率 —— 对账已闭环，覆盖率未达 85%

### 3.1 独立对账脚本（已交付）

`scripts/reconcile.py` 不 import pipeline，直接从一次运行的产物重算：

| 检查 | 内容 |
|---|---|
| 清洗后行数 | `cleaned_data.csv` 行数 == `metrics.quality.rows_out` |
| 行数守恒 | `rows_in − duplicates_removed − dropped_all_empty == rows_out` |
| 指标合计 | 每个 `target_totals.metrics` 与清洗列求和（或均值，按 plan 的 aggregation）一致 |
| 血缘行号 | `__source_row` 全部为正整数 |
| 填补标记 | `__imputed` 行数与 `missing_filled` 相容 |

真实运行对账结果（11 项全 PASS）：

```text
[PASS] 清洗后行数与 metrics 一致: csv=100 metrics=100
[PASS] 行数守恒: 101 - 1 - 0 = 100
[PASS] 指标合计对账 revenue/orders/customers/new_customers/cost/… 
[PASS] 血缘行号完整: 异常行号 0
[PASS] 填补标记与 metrics 相容: imputed_rows=10 填补值=30
对账通过：11 项
```

篡改 metrics 的合计或行数后脚本会失败（两条测试覆盖）。

### 3.2 覆盖率门槛（机制已交付，目标未达）

`scripts/coverage_gate.py`：零依赖（`sys.settrace` + AST 语句统计），本机离线可跑，
CI 同一套。**当前核心行覆盖率 60.3%**：

| 模块 | 覆盖率 | 模块 | 覆盖率 |
|---|---:|---|---:|
| analysis | 71.7% | readers | 71.7% |
| pipeline | 71.3% | schema | 70.4% |
| planner | 69.5% | report | 79.3% |
| cleaning | 74.2% | normalize | 62.2% |
| utils | 63.0% | services | 53.5% |
| config | 39.8% | discovery | 40.7% |
| cli | 32.8% | scheduler | 38.1% |

**门槛策略**：CI 先锁在 58%（只升不降），目标 85%。因此 P2-3 **只能算部分闭环**。

> 说明：`models.py` 显示 3.3% 是统计口径问题（dataclass 字段行在 import 期执行，
> 采集时点未覆盖），不代表真实缺口；修正采集起点后可消除该误差。

## 4. 结论与下一步

- **P2-1 ✅ / P2-2 ✅ / P2-3 ◐（对账完成，覆盖率 60.3% vs 85% 目标）**
- 系统整体仍为**有条件放行**：可日常使用、保留人工复核，不宣称无条件商业级。
- 剩余唯一工作：**测试补强**，把核心覆盖率从 60.3% 提到 85%（缺口主要在 cli / config /
  scheduler / discovery 分支与 analysis 的图表分支）。这是一项纯增量工作，不涉及行为变更。
- 建议顺序：先补 cli/config/scheduler 的纯函数与错误分支（低成本、覆盖率高产出），
  再补 analysis 各图表路径的参数化用例，最后把门槛从 58% 逐档提到 85%。
