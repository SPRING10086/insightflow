# LLM 阶段 2 交付记录：分析计划生成（双轨对照）

> 依据：《LLM 阶段 2 任务书：分析计划生成（双轨对照）》
> 结论：**计划产物已版本化、双轨落盘、字段闭环拦截生效；3 个验收样本均产出 LLM 计划，真实工商 2 个小表补验完成（1 个语义降级，如实记录）。默认报告与数字零变化。**

## 1. 架构

```text
核心                                   LLM 旁路
├─ planning.py  PlanProvider 契约      ├─ llm/plan_adapter.py  实现 PLAN
│  PlanDraft/PlanArtifact/diff/closure │─ llm/plan_contracts.py 结构+闭环校验
│  save_plan_artifacts                └─ llm/prompts/plan_v1.md
├─ semantic_assembly.build_plan_provider()  ← 同一唯一装配点（白名单不变）
├─ pipeline.execute(..., plan_provider=None)
└─ cli: run --plan {none,llm}
```

- 输入是阶段 1 的语义 Schema（有 LLM 轨则用 LLM 轨，否则规则轨）+ anomalies；
- 产物：`plan_rule.json` / `plan_llm.json` / `plan_diff.json`，`run_record.metadata.plan` 汇总；
- `plan_provider=None`（默认）时不调用、不落盘，`metrics/kpis/报告` 逐字节不变（对照测试）。

## 2. 防幻觉闭环（有测试 + 真实拦截证据）

`validate_plan_closure()` 强制：

- 指标必须是 `metric` 角色；维度必须是 `dimension`；text/id 不得当指标聚合；
- 图表 x 轴只能是 `dimension/date`（`histogram/scatter` 例外允许 `metric`），y 轴必须 `metric`，series 必须 `dimension`；
- 任一字段不在语义 Schema 中 → 直接 `ContractError`；`quality_risks` 空 → 拒绝；
- 禁止业务数字、图表数据槽必须为空。

**真实拦截**：02/03 的多轮调用中，模型把 `销售额/注册资本` 放到 x 轴、把 `统一社会信用代码` 当维度，被闭环拒绝并触发带反馈重试；修正 histogram/scatter 例外后 03 通过（这条例外本身就是模型暴露出的规则缺口）。

## 3. 三样本双轨结果（真实调用，2026-11-09）

| 样本 | 计划来源 | 指标 Jaccard | 维度 Jaccard | 图表 Jaccard | 质量风险数 |
|---|---|---|---|---|---|
| 01 主样本 | llm | 1.0 | 1.0 | 0.125 | 5 |
| 02 边界样本 | llm | 1.0 | 1.0 | 0.167 | 6 |
| 03 工商（合成） | llm | 1.0 | 0.0 | 0.0 | 5 |

- 指标集合与规则计划完全一致；维度在 03 上规则计划为空、LLM 给出了真实可分组维度（登记机关/经营状态/行业代码），属合理补充；
- 图表 Jaccard 低是预期：规则计划图表 x/y 固定取主维度/主指标，LLM 会按字段语义组合，需人工按 checklist 评审；
- 所有计划都带 `quality_risks`（干净样本也显式给出 5 条风险，如"未发现显著风险"之外的缺失/重复提示）。

差异报告：[docs/llm-plan-diff.md](llm-plan-diff.md) · 原始数据：[docs/llm-plan-diff.json](llm-plan-diff.json)

## 4. 真实工商数据补验（只读抽样）

- 数据源：`C:\Users\Acer\Downloads\csv\<年份>\<省份>.csv`；**只读前 300 行**到临时工程，原始文件未改动、未整表外传；
- 抽样：`2018/上海.csv`（target=2018-01-01，命中 6 行）、`2019/上海.csv`（target=2019-01-02，命中 13 行）；
- 观察：
  - 真实表 10 列、长文本（经营范围）、`注册资金` 混合 `500万人民币 / 1,000(万元人民币) / N/A`；
  - **2019 样本**：语义 LLM 轨成功，计划 LLM 轨成功；LLM 额外把 `所在省份` 纳入维度（规则计划没有）；
  - **2018 样本**：语义调用连续 3 次返回**结构不闭合的 JSON** → 降级为规则语义（`source_kind=fallback`），计划仍基于规则语义正常生成。这是模型输出稳定性问题，不是契约放宽；
  - 该降级不影响主流程：运行 `success`、报告照常产出。

## 5. 成本

| 项 | 金额 |
|---|---|
| 最后一轮语义（5 表） | 0.0431 元 |
| 最后一轮计划（5 次） | 0.0584 元 |
| 本阶段累计（含调试/重跑） | **约 0.45 元** |

单表 5 元、整批 20 元护栏均未触发；脱敏与载荷最小化未改动（仍为前 10 行 + 计数特征）。

## 6. 测试与门禁

```text
python -m unittest discover -s tests
Ran 187 tests  OK        （阶段 1 的 174 + 13 计划/闭环/降级/CLI）
```

新增 13 项：计划契约（合法、幻觉字段、text/id 当指标、metric 当维度、空风险、数据槽、业务数字、histogram 例外）、diff 计算、管线双轨落盘且数字不变、计划故障降级、干净数据风险陈述、CLI 无配置降级。

## 7. 未做 / 待审查方

1. **2018 真实样本的语义降级**：连续 3 次 JSON 不闭合；要彻底解决需要更强 JSON 约束（如服务端 json_schema）或更短输出，本轮未继续消耗调用。
2. 真实数据只抽了 2 个省/年小表，且按任务书只读前 300 行；更大样本的稳定性未验证。
3. 计划内容合理性（图表是否匹配指标/维度、问题是否有业务意义）需审查方按 checklist 人工评审；差异报告只做中性呈现。
4. GUI 只读预览、阶段 3 沙箱执行均未做。
