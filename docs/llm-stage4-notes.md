# LLM 阶段 4 交付记录：可视化 HTML 叙事（占位符 + 转义）

> 依据：《LLM 阶段 4 可视化 HTML 任务书》与设计评审意见。
> 结论：**模型只写带 `[[key]]` 占位符的文字，数字由本地确定性结果渲染；裸数字（含中文量化）、未知引用、5 类 XSS 全部被拒；默认关闭时报告零变化。3 个验收样本 + 1 个真实工商抽样叙事成功，占位符全部解析、无残留。**

## 1. 模块与链路

```text
core：narrative.py（协议、事实映射、校验、渲染、产物）
      report.render_report(..., narrative=None) → 仅在有叙事时插入独立区块
      semantic_assembly.build_narrative_provider（唯一装配点）
      pipeline.execute(..., narrative_provider=None)   cli: --narrative none|llm
llm： narrative_gen.py + prompts/narrative_v1.md
产物： narrative_facts / narrative_llm / narrative_render / narrative_rule.json
```

- 叙事区块标题「智能解读」，副标题「LLM 生成，仅供参考；数字来自确定性计算」；
- 默认 `None` 时不调用、不落盘、不插入区块；`run_record.metadata.narrative` 汇总来源/成本/占位符数/原因。

## 2. 占位符与事实映射（白名单）

语法 `[[namespace.key]]`，仅小写命名空间；映射表：

| 命名空间 | 键 |
|---|---|
| `meta` | `date` / `dataset_kind` / `rows` |
| `kpi` | `<key>.value` / `.label` / `.unit` / `.delta_pct` |
| `total` | `records` / `<canonical>` |
| `topn` | `<dimension>.first/second/third` / `.first_share` |
| `quality` | `score` / `duplicates` / `missing` / `outliers` |
| `metric`/`dimension` | `<canonical>.label` |
| `chart` | `<chart_id>.title` |

- 未知/越权引用、未闭合/嵌套/空格占位符、未知 `chart_id` → 渲染前拒绝；
- 空值事实不暴露，避免渲染出空串。

## 3. 安全实现

| 防线 | 规则 |
|---|---|
| 裸数字 | 剔除占位符后，阿拉伯/全角数字、`%`、`￥`、`$`、`元/万/亿` 前数字一律拒绝 |
| 中文量化 | `百分之X`、`X成`、`X倍`、`翻X番`、`第X` 拒绝；"一个趋势"等正常表述放行 |
| 标签/协议 | 禁 `<`、`>`、反引号、`javascript:`、`data:`、`vbscript:`、`on\w+=`、HTML 实体（`&lt;` 等） |
| 转义 | 字面量 `html.escape(quote=True)`；替换值仅允许数字/单位/中文/`¥`/常见标点（无字母、无尖括号） |
| 结构 | 渲染后断言无 `[[`/`]]` 残留；报告中 `<script` 数量与基线一致（测试锁定） |
| 长度 | 单段 ≤500 字、总段 ≤20、chart 叙事 ≤ 计划图表数 |

安全测试 15 项（`tests/test_narrative_security.py`）：裸数字（阿拉伯/全角/%/货币/中文量化）、占位符语法/未知/越权、5 类 XSS + 实体混淆、渲染转义、替换值白名单、事实映射白名单。

## 4. 降级矩阵（全部有测试）

| 触发 | 行为 |
|---|---|
| 配置/超时/JSON 不闭合 | 回退规则洞察，不渲染区块 |
| 裸数字 / 中文量化 | 拒绝，`reason=bare_number` |
| 占位符语法/未闭合 | `reason=syntax` |
| 未知/越权引用 | `reason=unknown_ref` |
| 标签/事件/协议 | `reason=unsafe_markup` |
| 渲染异常/残留占位符 | `reason=syntax`，丢弃渲染 |
| 默认关闭 | 无调用、无产物、报告零变化 |

运行始终 `status=success`，`metadata.narrative` 留痕。

## 5. 真实调用证据（2026-11-09）

| 样本 | 来源 | 占位符 | 成本 | 延迟 | 区块渲染 | 无残留 |
|---|---|---|---|---|---|---|
| 01 主样本 | llm | 15 | 0.0049 | 3.1s | 是 | 是 |
| 02 边界样本 | llm | 17 | 0.0039 | 2.5s | 是 | 是 |
| 03 工商（合成） | llm | 20 | 0.0059 | 2.8s | 是 | 是 |
| 真实 2018 上海（前 300 行） | llm | 17 | 0.0047 | 2.5s | 是 | 是 |

原始证据：[docs/llm-narrative-evidence.json](llm-narrative-evidence.json)。示例：`本期销售额为¥125,200.00元，较上期变化—；有效记录为10行，变化—。`（数字全部来自本地格式化）。

## 6. 测试

```text
python -m unittest discover -s tests
Ran 246 tests  OK        （阶段 3 的 227 + 15 安全 + 4 管线）
```

管线测试覆盖：默认关闭无区块/无产物、成功渲染且脚本数不变、两类失败回退（XSS/裸数字/未知引用/异常）、无叙事时报告内容确定性（仅归一化 runId/时间戳）。

## 7. 偏差与未解决项

1. **替换值允许 `¥`**：真实 KPI 格式化输出含 `¥`（如 `¥125,200.00`），白名单加入该符号；仍禁止字母与尖括号。
2. **确定性测试做了归一化**：报告本身含 `runId`/生成时间/步骤耗时，逐字节比较需归一化这些运行标识；叙事关闭时内容结构不变。
3. 真实 2018 语义轨仍可能 JSON 不闭合（本阶段叙事只依赖确定性结果，不受影响）。
4. 长文本成本未做压力测试；`dist/` 打包环境未验证。
5. 模型文案质量（是否空话、是否贴合业务）需审查方人工评审。
