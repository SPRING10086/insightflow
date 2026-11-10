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
3. **narrative 依赖 execute**：解析时若 `narrative` 非 shadow 而 `execute` 仍 shadow → narrative 降为
   shadow，原因 `upstream_execute_shadow`（数据流：叙事数字来自确定性结果，需执行链已确定）。

### narrative 采信落地

| 决议模式 | 行为 |
|---|---|
| `shadow`（默认） | 若配置了 provider，仍生成并落盘 `narrative_*` 产物用于对比，但**报告不插入“智能解读”区块** |
| `gated` | 叙事通过 `validate_narrative` + `render_narrative` 才插入区块；XSS/裸数字/未知引用失败 → 自动回退、`status=success`、`metadata.narrative.reason` 留痕 |
| `primary` | 与 gated 相同的校验门槛，失败仍回退规则洞察；`force_rules` 时一律不插入 |

> 迁移提示：阶段 5 中“开启 narrative feature 即渲染区块”，6b 起默认 `shadow` 不再渲染；
> 需要渲染时必须显式 `adoption.narrative = gated/primary`（且上游 execute 非 shadow）。

### 测试（新增 9 项）

- 环境变量覆盖配置（`0` 关掉配置 force、`true` 强制全 shadow）；
- `final_mode/downgrade_reason/force_rules` 结构；
- narrative 缺 execute 上游 → 降级且原因正确；
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


