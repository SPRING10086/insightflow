# LLM 阶段 4 可视化 HTML · 设计（待审查方确认后实现）

> 目标：LLM 只写"带占位符的叙事文本"，数字由本地确定性结果渲染；模型文本一律转义插入，页面不新增脚本/事件。
> 安全假设：**叙事是恶意的**；裸数字静态拒绝、占位符白名单、XSS 双保险、失败即回退。

## 1. 模块划分

```text
core/
├─ narrative.py        # NarrativeProvider 协议、事实映射、占位符解析/渲染、校验、产物
├─ report.py           # 新增可选参数 narrative=None；None 时输出逐字节不变
├─ pipeline.py         # execute(..., narrative_provider=None)；元数据/落盘
├─ semantic_assembly.py# 唯一装配点 build_narrative_provider()
└─ cli.py              # run --narrative none|llm
llm/
├─ narrative_gen.py    # 调模型 → 叙事 JSON 契约（不含数字）
└─ prompts/narrative_v1.md
产物（当日目录）：
├─ narrative_facts.json   # 暴露给模型的事实键（无值）
├─ narrative_llm.json     # 模型原始 JSON + 成本/延迟
├─ narrative_render.json  # 占位符→格式化值 的映射与渲染文本
└─ narrative_diff.json    # 规则洞察 vs LLM 叙事（中性对照）
```

- 叙事是**增强旁路**：不改变 KPI、对账结论与既有图表数据；默认 `None` 时零调用、零落盘。
- `chart_id` 绑定：叙事里的图表段落必须引用 `plan.charts[].chart_id`，未知 id 拒绝。

## 2. 占位符语法与事实映射（白名单）

**语法**：`[[namespace.key]]` 或 `[[namespace.key.attr]]`，仅 ASCII 小写命名空间 + `[A-Za-z0-9_\u4e00-\u9fa5-]` 键；不得嵌套、不得含空格、必须成对闭合。语法错误/未闭合 → 整条叙事拒绝。

| 命名空间 | 可用键 | 渲染值（本地格式化） |
|---|---|---|
| `meta` | `date`、`dataset_kind`、`rows` | 日期、数据集类型、有效行数 |
| `kpi` | `<canonical>.value`、`.delta_pct`、`.label` | KPI 数值/环比/标签（仅限本次实际生成的 KPI） |
| `total` | `records`、`<canonical>` | 总记录数、各指标总量 |
| `topn` | `<dimension>.first/second/third`、`.first_share` | Top3 分组键与占比 |
| `quality` | `score`、`duplicates`、`missing`、`outliers` | 质量分与清洗统计 |
| `metric` / `dimension` | `<canonical>.label` | 字段中文名 |
| `chart` | `<chart_id>.title` | 既有图表标题 |

- **不暴露**：原始明细、未进入分析的字段、任意未列出的键；未知引用/越权引用 → 拒绝。
- 模型 prompt 只收到上述**键名清单**（不含值）+ 字段结构 + 前 10 行脱敏样本；数字一个都不发。

## 3. 裸数字与内容校验（静态、可执行）

对每段文本（标题/段落/要点）：

1. 移除合法占位符后，检测 `[0-9０-９]`、`%`、`％`、`￥`、`$`、`元/万/亿` 紧跟数字 → 命中即 **BareNumberError**；
2. 占位符正则之外的 `[[`/`]]`、未闭合、空引用 → **SyntaxError**；
3. 每个引用必须在事实白名单内解析出真实值 → 否则 **UnknownReferenceError**；
4. 文本禁止 `<`、`>`、反引号、`javascript:`、`on\w+=`、`data:`、`vbscript:`（不区分大小写）→ **UnsafeMarkupError**；
5. 单段长度 ≤ 500 字、总叙事 ≤ 20 段、chart 叙事 ≤ 计划图表数。

## 4. XSS 防线（双保险 + 结构校验）

1. **静态拒绝**：模型文本出现标签/事件/协议即拒绝，不进入渲染。
2. **统一转义**：渲染时按占位符切分，字面量走 `html_escape(text, quote=True)`；占位符替换值只允许本地格式化器产物（正则 `^[-\d.,%元单人]+$`），不拼接模型内容。模型文本从不进入 HTML 属性或标签名。
3. **结构校验**（渲染后）：
   - `<script` 数量与模板基线一致、`on\w+=` 数量不增加；
   - 无残留 `[[`/`]]`；
   - 渲染文本不含 `<`/`>` 之外的注入向量；
   - 任一项失败 → 整份叙事回退规则洞察，页面按原样渲染。
4. **注入测试（5 类）**：`<script>alert(1)</script>`、`" onmouseover=alert(1) x="`、`javascript:alert(1)`、`&lt;script&gt;alert(1)&lt;/script&gt;`、`<img src=x onerror=alert(1)>`，断言全部被拒/转义且页面脚本数不变。

## 5. 降级矩阵

| 触发 | 处理 | 留痕 |
|---|---|---|
| 配置缺失、429、超时、JSON 不闭合 | 不渲染叙事，回退规则洞察 | `reason=request/json`, 原始片段截断 |
| 裸数字 | 拒绝整条叙事 | `reason=bare_number`, 命中片段 |
| 占位符语法/未闭合 | 拒绝 | `reason=syntax` |
| 未知/越权引用 | 拒绝 | `reason=unknown_ref:<key>` |
| 危险标记（标签/事件/协议） | 拒绝 | `reason=unsafe_markup` |
| 渲染后结构校验失败 | 丢弃渲染结果 | `reason=structure` |
| `narrative_provider=None`（默认） | 不调用、不落盘 | 报告逐字节不变（测试锁定） |
| 任一步异常 | `status=success`，报告照常 | `metadata.narrative.reason` |

## 6. 测试计划

**单元**：占位符解析（合法/嵌套/未闭合/空格）、裸数字（阿拉伯/全角/%/￥）、未知引用、chart_id 绑定、渲染切分与转义、长度上限。
**安全（5 类 XSS）**：见 §4；另加 HTML 实体混淆与属性逃逸变体。
**管线**：假 provider 成功 → HTML 含转义文本、占位符全部替换、数字与 `metrics.json` 一致、脚本/事件数不变；裸数字/未知引用/危险标记 → 回退且 status=success；默认关闭时报告 SHA-256 与现状一致。
**真实调用**：3 个验收样本 + 1 个真实工商 300 行；逐段核对每个占位符解析值 = 报告数字；登记成本/延迟/失败原因。
**门禁**：Python310 全量（现 227 + 新增）、核心覆盖 ≥85%。

## 7. 待确认点

1. 叙事版式：新增独立"智能解读"区块（推荐，失败时不渲染即回到现状）还是改写既有洞察卡？
2. 是否允许文本出现 `<`（本设计禁止，最稳；若业务确需，改为仅允许 `&lt;` 字面量）。
3. 段落上限（500 字/段、20 段）与图表叙事数量是否合适。
4. 事实映射是否还要暴露 `chart.<id>.type` / `kpi.<key>.unit`（本设计暴露 label，不暴露 unit）。
