# LLM 阶段 1 交付记录：语义 Schema 双轨接入

> 依据：《LLM 阶段 1 任务书：语义 Schema 正式接入（双轨旁路）》
> 结论：**语义 Schema 已版本化落盘、按旁路双轨接入；真实调用一致率 100%（20/20）、关键角色 0 错误、异常召回 100%、单次评估成本 0.0372 元。** 默认报告与主流程行为不变。

---

## 1. 架构：注入 + 唯一装配点

```text
核心（无网络）                        LLM 旁路（llm/）
├─ semantic.py  ← 中立契约            ├─ adapter.py  implements SemanticProvider
│  TableSample / FieldSemantics       │  载荷→调用→契约→覆盖校验→重试
│  SemanticProvider (Protocol)        └─ client.py（唯一 urllib）
│  SemanticArtifact / diff / save
├─ semantic_assembly.py  ← 唯一允许引用 llm 的装配点（白名单）
├─ pipeline.py  execute(..., semantic_provider=None)
└─ cli.py       run --semantic {none,llm}
```

- **依赖注入**：`pipeline` 只接收 `SemanticProvider` 实例，不 import `llm`；`schema/analysis/report` 零改动。
- **隔离白名单精确放开**：测试从"核心/GUI 一律禁止"改为"**恰好只有 `semantic_assembly.py` 可引用 llm**"，其余文件仍禁止；`llm/client.py` 仍是唯一使用 `urllib` 的文件。
- **默认为零**：`semantic_provider=None` 时，不调用、不落盘、`metrics/kpis/report` 逐字节不变（有对照测试）。

## 2. 双轨产物（落盘、版本化、可审计）

每次启用时写入 `output/<日期>/`：

| 文件 | 内容 |
|---|---|
| `semantic_rule.json` | 规则轨语义（来源 `rule`，置信度 1.0） |
| `semantic_schema.json` | LLM 轨语义（来源 `llm` / `fallback` / `mixed`），含每表 `anomalies` |
| `semantic_diff.json` | 对照：`role_agreement`、`unit_agreement`、`key_role_errors`、逐字段 mismatch |

产物结构（节选）：`schema_version`、`prompt_version`、`generated_at`、`provider`、`source_kind`、`cost_cny`、`latency_ms`，每表 `fields[]` 含 `name/role/unit/meaning/confidence/source/relations`。

运行记录 `run_record.json` 的 `metadata.semantic_schema` 汇总来源、一致率、成本、耗时与产物文件名。

## 3. 降级与成本（均有故障注入测试）

- **逐表降级**：provider 抛任何异常 → 该表回退规则语义、`source_kind=fallback`、错误写入 `error` 字段，并追加 `[semantic_fallback]` 告警；**运行仍 `success`，报告照常产出**。
- **配置缺失降级**：`run --semantic llm` 但无 `.env`/密钥 → 打印"已降级为规则口径"，退出码 0，不产语义产物。
- **成本**：单表 5 元硬停沿用（每表新建 client）；新增**整批 20 元预算**，用尽即 `CostLimitExceeded` → 后续表降级。
- **覆盖校验 + 一次重试**：模型漏字段/多字段时，带反馈重试一次；两次不过则抛错并降级（避免"静默缺列"）。

## 4. 一致率实测（2026-11-09，真实调用，golden 已由审查方确认 `reviewer-confirmed`）

| 样本 | 角色一致 | 关键角色错误 | 异常召回 | 异常精确率 | 成本 | 延迟 |
|---|---|---|---|---|---|---|
| 01 主样本（干净） | 5/5 = 100% | 0 | 100% | **100%** | 0.0109 元 | 8.9 s |
| 02 边界样本（含问题） | 5/5 = 100% | 0 | 100% | 25% | 0.0110 元 | 9.2 s |
| 03 工商登记（合成） | 10/10 = 100% | 0 | 100% | 33% | 0.0153 元 | 10.5 s |
| **合计** | **20/20 = 100%** | **0** | **100%** | 均值 52.8% | **0.0372 元** | 均 9.6 s |

> 精确率口径见 §7：02 的四个预测都是样本中可见的真问题（重复行、999%、50000、日期格式混用），
> 但 golden 仅列了销售额；03 的 `经营状态=吊销` 是真问题，`企业名称` 疑似误报。golden 异常清单待审查方扩充。

原始结果已落盘入库：[docs/llm-semantics-eval.json](llm-semantics-eval.json)（含 `golden_status`、`prompt_version=analysis_plan_v3`、逐样本 mismatch 与 anomalies）。

- 门槛：角色一致率 ≥85%（目标 90%）→ **100%**；date/metric/id 关键角色零错误 → **达到**。
- **离群点召回**：阶段 0 为 4/5（漏报 `销售额=50000`）；本阶段 3/3（02 的销售额、毛利率、订单号重复均被点名；03 的注册资本被点名），**有提升且给出数字**。
- 本轮 01 未触发重试；重试机制本身仍保留（漏字段/坏 JSON 时最多 3 次）。
- 03 的 `企业名称 / 法定代表人` 已按审查方裁定保持 `text`；v3 通过载荷中的 `unique_ratio/unique_count` 统计特征 + 判定规则修正了"非数字即维度"的过度泛化，**03 从 8/10 提升到 10/10**。

> 本阶段真实调用累计约 **0.5 元**（含早期调试、多轮重跑与稳定性验证），全部低于单表 5 元 / 整批 20 元护栏。

## 5. 复现命令

```powershell
$env:PYTHONPATH="src"

# 离线：只看脱敏载荷
python -m insightflow.llm plan "examples/acceptance/02_边界样本_含问题.csv" --dry-run

# 主流程旁路（写 3 份语义产物；报告仍用规则语义）
python -m insightflow run --date 2026-11-07 --force --semantic llm

# 一致率评估（真实调用；结果写 logs/llm_semantics_eval.json）
python scripts/llm_semantics_eval.py
```

## 6. 测试

```text
python -m unittest discover -s tests
Ran 174 tests  OK        （原 163 + 9 双轨/降级/隔离 + 2 基数统计/提示词）
```

新增 9 项覆盖：规则/LLM 产物生成与 diff、装配与版本字段、适配器成功路径、整批预算熔断、漏字段重试、两次失败、管线双轨落盘且数字不变、provider 故障降级、CLI 无配置降级。

## 7. 未做 / 待审查方

1. **golden 最终版**：现为草稿；审查方确认后替换，我再复跑一次数字。
2. **真实工商数据**：仓库内没有工商数据集，03 是**合成样本**（高基数文本 + 不规范注册资本）；若需真实抽样，请提供 2–3 个小表。
3. **examples/raw 批量真实调用**：本阶段只跑 3 个验收样本；批量验证（xlsx 先转 csv）属 P2。
4. 阶段 2（分析计划）、阶段 3（沙箱执行）、GUI 人工确认界面均不在本阶段范围。
