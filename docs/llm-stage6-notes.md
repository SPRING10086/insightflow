# LLM 阶段 6 交付记录：灰度切换

## 6a 采信模式框架（2026-11-10）

### 实现

- **配置扩展**（`LLMSettings`）：
  - `force_rules: bool`（默认 false）；
  - `adoption: {semantic, plan, execute, narrative}`，取值 `shadow / gated / primary`，**默认全部 shadow**；
  - fail-closed：未知能力、未知模式、非布尔 `force_rules` → `ConfigError`；非法配置整体关闭 LLM。
- **模式解析**（核心纯模块 `adoption.py`，无网络/无 llm import）：
  - `force_rules` 或环境变量 `INSIGHTFLOW_FORCE_RULES=1` → 全部 shadow（kill switch）；
  - 总开关/功能开关关闭 → 该能力 shadow（原因 `llm_disabled` / `feature_off`）；
  - **依赖约束**：`plan` 非 shadow 要求 `semantic` 非 shadow；`execute` 非 shadow 要求 `plan` 非 shadow，
    违反时自动降为 shadow 并记录 `upstream_semantic_shadow` / `upstream_plan_shadow`。
- **接入**：`pipeline.execute(..., adoption_decision=None)`；仅当存在非 shadow 或 force_rules 时写入
  `run_record.metadata.adoption`（模式 + 降级原因），**默认全 shadow 不写任何新字段**。
- CLI 与 GUI 服务均在运行前调用 `resolve_adoption(settings.llm)` 并传入。

### 零行为变化证据

- 同一数据两次运行：`metrics.json`、`cleaned_data.csv`、`analysis_plan.json` **逐字节相等**；
- 默认配置下 `run_record.metadata` 不含 `adoption`；
- 非 shadow 配置只写决议，不构建任何 provider、不采购任何 LLM 产物（测试断言无 `narrative_llm.json`）。

### 测试（13 项，先红后绿）

配置默认/非法值、feature 关闭强制 shadow、plan/execute 依赖降级、依赖链通过、`force_rules` 与
环境变量 kill switch、全 shadow 零变化、非 shadow 仅记录决议。

### 已知边界

- 6a 只做“决议”，**不改变报告消费路径**；真正的门槛判定与采信在 6b 按 narrative → execute → plan → semantic 顺序落地。
- `force_rules` 为 true 时仍允许（若配置了）LLM 调用做旁路落盘，但零采信（6b 实现运行时）。

## 6b-1 复核对齐 + narrative 采信（2026-11-10）

### 三项复核意见对齐

1. **环境变量优先于配置**：`INSIGHTFLOW_FORCE_RULES` 显式设置时覆盖 `force_rules` 配置——
   `1/true/yes/on` 强制全 shadow；显式 `0/false` 连配置里的 `force_rules=true` 也会被关闭；
   未设置时才读配置。
2. **`metadata.adoption` 统一结构**：
   ```json
   {"force_rules": false, "final_mode": {"semantic": "..."}, "downgrade_reason": {"plan": "upstream_semantic_shadow"}}
   ```
   6b 各模块直接消费 `final_mode` / `downgrade_reason`。
3. **narrative 独立采信（复核 issue 2 纠正）**：narrative 的 facts 来自确定性 `build_facts`，不依赖
   execute 采信；已移除 6b-1 中“narrative 依赖 execute”的强制降级，narrative 可单独开 gated/primary，
   保留其自身 `validate_narrative` 门槛。

### narrative 采信落地

| 决议模式 | 行为 |
|---|---|
| `shadow`（默认） | 若配置了 provider，仍生成并落盘 `narrative_*` 产物用于对比，但**报告不插入“智能解读”区块** |
| `gated` | 叙事通过 `validate_narrative` + `render_narrative` 才插入区块；XSS/裸数字/未知引用失败 → 自动回退、`status=success`、`metadata.narrative.reason` 留痕 |
| `primary` | 与 gated 相同的校验门槛，失败仍回退规则洞察；`force_rules` 时一律不插入 |

> 迁移提示：阶段 5 中“开启 narrative feature 即渲染区块”，6b 起默认 `shadow` 不再渲染；
> 需要渲染时必须显式 `adoption.narrative = gated/primary`（narrative 可独立采信，不依赖 execute）。

### 测试（新增 9 项）

- 环境变量覆盖配置（`0` 关掉配置 force、`true` 强制全 shadow）；
- `final_mode/downgrade_reason/force_rules` 结构；
- narrative 可独立采信（不要求 execute 非 shadow）；
- shadow 不渲染但保留产物、gated 渲染、gated 下 XSS 回退、force_rules 阻止采信、primary 无 provider 安全。

## 6b-2 execute 采信（过渡方案 b，2026-11-10）

### 采信门槛（全部满足才采信）

1. `adoption.execute != shadow`；
2. `reconcile(rule, llm).accepted == True`；
3. `source_kind == "llm"`、`memory_guard == "job"`、无 `error`；
4. 计划存在主指标且对账结果含 `total_llm`。

失败时 `metadata.execution.adopted=false`，并记 `gate_reason`
（`reconcile_mismatch` / `source_fallback` / `memory_guard` / `execution_error` / `no_primary_metric`），报告数字仍为规则结果。

### 采信范围（方案 b 的明确边界）

- **只采信已对账的主指标合计**：覆盖 KPI 中 `key == plan.primary_metric` 的 `value/formatted`；
  `formatted` **直接复用 analyze 的 `_format_value(value, _value_type(metric, fields))`**，
  不再自维护格式化（修复复核 P1：number 被取整、percent 漏乘 100）；
- 执行聚合按字段 `aggregation` 进行：`SUM` 求和、`AVG` 求均值（百分比/比率字段为 AVG），
  `rule_execute` 与代码生成提示词同步，保证 percent 类主指标对账与展示口径一致；
- **其余指标与所有分组/图表仍为规则计算**；报告顶部插入“数据来源”说明：
  「主指标合计（销售额）由 LLM 沙箱计算并已与规则对账；其余指标（…）与所有分组图表由规则引擎计算。」
- `metadata.execution` 增加 `adopted / adopted_metrics / rule_metrics / gate_reason`，供 6c 统计与审计；
- 方案 (b) 是过渡态：**6d 放量前必须升级为 (a) 全指标对账**（已在阶段 6 评审裁定 2 中锁定）。

### 测试（新增 5 项）

- gated + 对账通过：KPI 采用沙箱 total（含 5e-7 差异证明真替换）、报告含“数据来源”、metadata 标记 adopted；
- 对账不一致（+100）→ 不采信、gate_reason 正确、无来源说明；
- 执行失败 → 不采信、运行仍成功；
- `force_rules` → 不采信；
- narrative 回退 vs 纯规则：KPI/洞察逐字段一致（仅多一条回退告警），两者均无“智能解读”。

## 6b-3 plan 采信（2026-11-10）

### 核心侧纯函数适配器

`planning.adapt_plan(draft, fields, ...) -> AnalysisPlan`：

- 纯函数：无网络、无副作用、不 import `llm`；
- 校验并拒绝：未知字段、非 metric 当指标、非 dimension 当维度、无可用指标、不支持的图表类型；
- 映射：`metrics/dimensions` → 计划列表；首个 metric/dimension 作为主指标/主维度；
- 图表类型 → analyze 既有 step：`bar→rank`、`line→trend`、`donut/pie→share`、`pareto`、`histogram→distribution`、
  `heatmap`、`scatter`、`dod`；始终保留 `kpi` step；
- 产出 `AnalysisPlan` 交给**未修改**的 `analyze()` 计算所有数字；
- 已知边界：图表标题/轴字段仍由 analyze 按主指标/主维度生成，适配器只选择“图表种类 + 主指标/维度”。

### 管线采信

- 门槛：`adoption.plan != shadow` + 计划来源 `llm` + 适配器成功；
- 成功：`plan` 替换为适配计划，KPI/图表按 LLM 计划的主指标/维度生成；
- 失败：保留规则计划，`metadata.plan.gate_reason = plan_invalid:<原因>`，`status=success`；
- `metadata.plan` 记录 `adopted / adopted_primary / gate_reason`；
- 报告顶部“数据来源”增加：“分析计划由 LLM 提供（主指标：xxx）”。

### 测试（10 项，先红后绿）

适配器：正常映射、未知指标、角色错配（metric↔dimension）、无指标、不支持图表；
管线：gated 后 KPI 主指标变为 LLM 选择的订单量并带来源说明、非法计划回退规则且留 gate_reason、
shadow 不采信、`force_rules` 不采信。

## 6b-4 semantic 采信（2026-11-10）

### 核心侧纯函数门禁

`semantic_adoption.adopt_semantic_fields(artifact, table_schemas, rule_fields)`：

- **覆盖完整**：每张表的所有业务列（排除 `__` 血缘列）都必须在 LLM 语义中出现，缺一列即整次回退
  （`missing_column`）；
- **关键角色不冲突**：规则判为 `date/id/metric` 的字段，LLM 角色必须一致，否则 `role_conflict` 整次回退；
  非关键角色允许调整（如 dimension → metric），并记入 `changed` 列表；
- **单位冲突**：规则与 LLM 都给出非空单位且不同 → `unit_conflict` 整次回退；
- **可映射**：以规则 `column_map` 把原始列映射到 canonical；规则已丢弃的高基数列只允许 LLM 标为
  `text/id`，若声明为 metric/dimension/date → `unknown_canonical` 回退；未知 canonical 不进入后续计算；
- 采用 `dataclasses.replace` 生成新 FieldSpec，canonical/label/aggregation/value_format 仍来自规则，
  **数字计算口径不变**。

### 管线采信

- 时机：清洗后、`build_plan` 前；成功时用采信后的字段集替代 `analysis_fields` 驱动计划与 `analyze()`；
- 失败：保留规则字段，`metadata.semantic_schema.gate_reason = semantic_invalid:<reason>`，`status=success`；
- 留痕：`metadata.semantic_schema` 增加 `adopted / adopted_changes / gate_reason`；
- 报告顶部增加“字段语义由 LLM 识别”；与 plan/execute 来源说明可叠加。

### 测试（9 项）

适配器：角色一致采信、缺列、关键角色冲突、单位冲突、未知 canonical 声明为 metric、dimension→metric 允许；
管线：gated 采信并显示来源、角色冲突回退且留 gate_reason、shadow 不采信。

## 6c 灰度观测（2026-11-10）

### 统计口径（`scripts/llm_stats.py`）

- 每能力：模式分布（shadow/gated/primary/absent）、`attempted/adopted/fallbacks`、
  `adoption_rate`、`fallback_rate`、`gate_reason` 分布；
- 一致性：`semantic_role_agreement_avg`、`plan_metrics_jaccard_avg`；
- 执行：`reconcile_attempted/passed/pass_rate`；
- 成本与稳定性：cost、json_failure_rate、retries、prompt_warn；
- 样本：`samples`（默认按不同数据指纹，灰度脚本按真实数据类别传入）；
- 放量判据：`runs ≥ 10`、`samples ≥ 3`、整体回退率 `≤5%`、对账通过率 `100%`；
  任一不满足即 `ready=false` 并给出 blockers。

### 灰度实跑（4 类样本 × 3 次 = 12 次，四能力全 gated）

| 能力 | 采信/尝试 | 回退原因 |
|---|---|---|
| semantic | **7/12** | `unit_conflict` ×3（03 表 注册资本 单位“万/万元”不一致）、`source_fallback` ×2（真实 2018 JSON 不闭合） |
| plan | **9/12** | `plan_invalid: 指标引用不存在字段: 登记机关` ×3（03 表该列未进入分析字段） |
| execute | **12/12** | 无；对账通过率 **100%** |
| narrative | **10/12** | `unknown placeholder: total.rows` ×1；`UnsafeMarkupError: replacement value ...` ×1（事实值含字母） |

- 12 次运行全部 `status=success`，JSON 失败率 0，成本 **0.184 元**；
- 规则结果继续作为对照落盘（`semantic_diff` / `plan_diff` / `execution_diff`）；
- 原始证据：[docs/llm-stage6-gray.json](llm-stage6-gray.json)。

### 放量判定（6c → 6d）

```text
runs=12 (≥10) · samples=4 (≥3) · reconcile_pass_rate=100%
overall_fallback_rate=20.83% > 5%  → ready=false
blockers: ["fallback_rate 0.2083 > 0.05"]
```

**结论：维持 gated，不进入 primary。** 达标前需先降低非预期回退：

1. **单位口径**：统一“万/万元”等写法（提示词/规则单位归一），否则 03 类工商表持续 `unit_conflict`；
2. **计划字段范围**：提示词限制只能引用实际分析字段，或让适配器把被规则丢弃的维度显式排除并提示；
3. **叙事事实值**：`SAFE_VALUE_RE` 不含字母，事实值含“LLM/英文”时会回退；需在 facts 构造侧规范化或提示词规避 `total.rows` 等不存在键；
4. **真实数据 JSON 稳定性**：仍偶发不闭合，需继续观察/服务端 json_schema。

> 灰度观测只统计与落盘，不改变默认 shadow 行为；`force_rules` 仍可一键回全规则。

## 6c 复核 P1 修复：沙箱默认内存 512 → 1024

复核实测 worker 导入 pandas 后基线 commit 524–530MB，512MB Job 上限低于基线，默认配置在系统紧张时间歇失败
（安全回退可靠、无错误数字，但冒烟不稳定）。修复：

- `LLMSettings.memory_mb`、`run_sandbox`、`ExecutorSettings.memory_mb`、`build_execution_provider`
  默认统一为 **1024**；`config.example.json` 同步；合法范围仍 64–4096；
- 冒烟脚本改用默认值（不再显式传 512），覆盖真实默认路径；
- 新增 `test_default_memory_mb_succeeds`：不传 memory_mb 跑最小聚合并断言结果，消除默认值成功路径盲区；
- 两阶段 Job Object、AST 白名单、输入/输出上限等护栏不变，只调整内存上限数值。

验收：全量 330 测试 OK；便携包冒烟连续 3 次 17/17（见下）。

## 6d 任务 1：execute 对账 (b) → (a) 全指标对账（2026-11-11）

### 契约扩展

- `ExecutionSpec.metrics: list[MetricRequest]`（metric/label/aggregation）；`requested_metrics()` 统一取集合；
  单指标旧调用（不传 metrics）仍输出 `{"total": ...}`，向后兼容。
- `rule_execute` 逐指标按各自 `aggregation`（SUM/AVG）计算 total，并逐指标产出 `metric_groups`；
  `groups` 保留主指标分组供旧接口读取。
- 沙箱输出新增**可选** `metric_groups`（每指标 `{columns, rows}`），校验沿用同一套行数/字节/有限数限制；
  `metrics` 仍为每指标一个数字，AST 白名单、Job、输入输出上限不变。
- `reconcile` 逐指标对账：total 用 `TOTAL_TOLERANCE`，分组键与分组值用 `RATIO_TOLERANCE`；
  缺指标、NaN/Inf、非数值、分组键缺失/多余均记 problems、`accepted=false`。
- 采信 **all-or-nothing**：任一指标对账不过 → 整次不采信、回退规则、`gate_reason=reconcile_mismatch`；
  全部通过 → 所有被覆盖 KPI（按 canonical key）统一替换为沙箱数字，格式化复用 analyze 口径。

### 覆盖范围

- 执行/对账覆盖 `plan.metrics` 中的全部 metric 字段（与 KPI 口径一致）；派生指标（如客单价）仍由规则计算，
  报告“数据来源”逐项列出 LLM 采信指标与规则指标。
- `metadata.execution` 增加 `metric_count` 与逐指标 `metrics` 明细（rule/llm/ok）。

### 测试与独立复算

- 单测 11 项：多指标规则基线、整体一致、**逐指标** total/分组值/分组键篡改、缺指标、NaN、非数值、
  管线双指标采信、单指标失败阻断全部采信；provider 级多指标代码 + `metric_groups` 解析。
- 独立复算脚本 `scripts/execution_reconcile_check.py`：纯 pandas 复算 total/分组，逐项一致；
  7 个篡改场景全部被拒；证据 `docs/llm-stage6-6d-task1-check.json`（`all_checks_pass=true`）。

## 6d 任务 1 整改 A：execution prompt 去循环（P1）

复核发现 prompt 示例用 `for` 循环、循环变量 `_m` 以下划线开头，与沙箱白名单（拒绝 `ast.For`、拒绝
`_` 前缀）自相矛盾，LLM 照写必被拒→重试→回退。

修复：

- 示例改为**逐指标显式展开**（无循环、无下划线变量），`metrics`/`metric_groups` 直接列出全部指标；
- 指令与自检明确"禁止显式循环语句；指标多时逐指标显式展开"，不再出现鼓励循环的表述；
- 新增 `tests/test_execution_prompt.py`：
  1. prompt 全文不含 `for ` / `while `；
  2. 示例代码不含 `_` 前缀名字；
  3. 从 prompt 解析出的 `code` 通过 `validate_code`；
  4. 该 code 在 `run_sandbox` 中产出两指标 metrics 与 metric_groups；
  5. 反向断言 `for` 循环仍被 `validate_code` 拒绝（白名单未放宽）。

## 6d 任务 1 整改 B：`_win_job` Job 句柄泄漏（P2）

复核发现 `_win_job` 创建 Job 句柄后四条路径均未关闭，每次调用净泄漏 1 个句柄
（GetProcessHandleCount 实测 8 次 +8）。

修复：

- `CreateJobObjectW / SetInformationJobObject / OpenProcess / AssignProcessToJobObject / CloseHandle`
  全部显式声明 `argtypes/restype`（句柄 `wintypes.HANDLE`、布尔 `wintypes.BOOL`），消除 64 位截断隐患；
- `try/finally` 统一句柄生命周期：成功路径同时关闭进程句柄与 job 句柄，三条失败路径也只关闭已成功创建的句柄；
- Job 无 `KILL_ON_JOB_CLOSE`，关闭句柄不会终止已加入的 worker；LimitFlags 仍为 `0x100`，
  两阶段装配、内存上限、Popen 参数、返回 bool 语义均不变。

回归测试 `tests/test_sandbox_job_handles.py`（Windows 实跑，非 Windows 跳过）：

- 显式签名 `GetProcessHandleCount`，预热后基线，连续 8 次 `_win_job`（每次真实子进程）；
- 净增量容差 ±2，不随次数线性增长；每次返回 True、子进程正常退出；
- **红→绿证据**：同一测试在基线 `51fea19` 上净增 **+8**（失败），修复后净增 ≤2（通过）。









