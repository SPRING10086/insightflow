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
