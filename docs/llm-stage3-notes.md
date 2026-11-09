# LLM 阶段 3 交付记录：受控执行（生成代码 → 沙箱 → 对账）

> 依据：《LLM 阶段 3 受控执行任务书》与设计评审意见。
> 结论：**LLM 只生成 pandas 代码，数字由本地沙箱执行；AST 白名单 + 子进程 + Job Object + 严格输出校验 + 规则对账全链路生效；7 类故障注入全部被拦或安全降级；3 个验收样本 + 1 个真实工商抽样执行成功且对账通过。**

## 1. 模块与调用链

```text
core：execution.py（ExecutionProvider 协议、规则基线、对账、产物）
      semantic_assembly.build_execution_provider（唯一装配点）
      pipeline.execute(..., execution_provider=None)   cli: --execute none|llm
llm： codegen 在 executor.py；sandbox.py（AST+子进程+Job）；sandbox_worker.py
产物： execution_rule / execution_code / execution_llm / execution_diff.json
```

- 执行对象是**清洗后的 canonical 列**（revenue/region…），不是原始表头；
- 默认 `None`：不调用、不落盘，报告与数字逐字节不变（有对照测试）；
- `--execute llm` 自动补建语义 + 计划 provider（否则沙箱没有可信口径）。

## 2. 安全实现（按"代码是恶意的"假设）

| 层 | 实现 |
|---|---|
| AST 黑名单优先 | 拒 `Import/ImportFrom/Lambda/FunctionDef/ClassDef/With/Try/For/While/Yield/Global/Nonlocal/Delete/Starred`；**任何 `_` 开头的 Name/Attribute 一律拒绝**（dunder 全家族） |
| 白名单 | 内建仅 `len/min/max/sum/round/abs/int/float/str/sorted/bool`；`df/pd/np` 属性方法白名单；`GroupBy.apply` 只接受字符串常量 |
| 字段闭环 | `df["X"]` / `df.loc[:, "X"]` / `groupby("X")` 的字符串必须是真实列，否则 `hallucinated_field` |
| 命名空间 | worker 内 `__builtins__` 为白名单 dict；不注入 os/sys/open；仅通过 stdin 管道接收父进程数据 |
| 进程隔离 | 独立子进程；墙钟超时 10s → `taskkill /T /F` 杀进程树；**两阶段**：worker 先导入 pandas 并回 READY，父进程再加 Job Object（512MB），避免把 import 算进用户代码配额 |
| 输出校验 | 只接受 `{"metrics", "groups"}` 两键；数值必须有限（拒 NaN/Inf/bool）；指标 ≤200、分组行 ≤5000、JSON ≤1MB；超限/非 JSON 一律拒绝 |
| 规模 | 输入 >200,000 行 → **执行前降级**（绝不截断后当全表结论） |

## 3. 对账（采信前提）

- 同一份清洗数据、同一口径：`total = sum(metric)`，分组 = 维度求和 top50；
- 总量容差 `1e-6`，分组值 `1e-4`，分组键集合必须完全一致；
- 对不上 → `accepted=false`，LLM 数字不采信，产物留痕（报告仍用规则结果）。

## 4. 7 类故障注入（真实执行，非 mock）

| # | 注入 | 结果 |
|---|---|---|
| 1 | `import os` | AST 拒绝，worker 未启动 |
| 2 | `df.__class__.__mro__` | AST 拒绝（下划线属性） |
| 3 | `open()` / `exec()` | AST 拒绝 |
| 4 | `while True`（绕过校验验证运行时） | 1s 超时，taskkill 杀进程树；随后再跑一次成功（无孤儿） |
| 5 | `[0]*10**8` + 64MB 配额 | Job Object 终止，`SandboxResourceError` |
| 6 | 非 dict / NaN / 多余键 / bool 指标 / 超限 | 输出校验拒绝 |
| 7 | `df["幽灵列"]` | 执行前 `hallucinated_field` 拒绝 |

另测：`pd.io...`、`"".join()`、`.str` 访问器、`apply(sum)`、`apply(lambda)`、`map` 类绕过均被拒。

## 5. 真实调用证据（2026-11-09）

| 样本 | 状态 | 执行来源 | 对账 | 规则总量 | LLM 总量 | 分组键 | 成本 | 代码延迟 |
|---|---|---|---|---|---|---|---|---|
| 01 主样本 | success | llm | **通过** | 125,200 | 125,200 | 5/5 | 0.0033 | 6.1s |
| 02 边界样本 | success | llm | **通过** | 50,500 | 50,500 | 3/3 | 0.0028 | 6.0s |
| 03 工商（合成） | success | llm | **通过** | 125,977 | 125,977 | 11/11 | 0.0044 | 6.1s |
| 真实 2018 上海（前 300 行） | success | llm | **通过** | 22,700,000 | 22,700,000 | 2/2 | 0.0034 | 6.1s |

- `memory_guard=job` 在四例中均为 `job`（Job Object 真实生效）；
- 真实 2018 样本的语义轨本轮因模型 JSON 不闭合降级一次，但执行仍基于规则语义完成并对账通过；
- 真实 2019 样本的计划未选出主指标，因此不进入执行（如实记录，未强造指标）；
- 原始证据：[docs/llm-execution-evidence.json](llm-execution-evidence.json)。

## 6. 测试

```text
python -m unittest discover -s tests
Ran 224 tests  OK        （阶段 2 的 204 + 20 执行/AST/故障注入/管线）
```

新增 20 项：AST 表驱动允许/拒绝、链根与 apply 专项、7 类注入、输入超限降级、默认关闭零行为、双轨落盘且数字不变、对账不一致、provider 故障降级、CLI 无配置降级。

## 7. 与设计的两处偏差（如实登记）

1. **内存配额采用两阶段**：512MB Job 在 pandas 导入后才施加；若先施加，pandas 自身导入就会 MemoryError（实测 256/512MB 均失败，1024MB 才够）。这样配额只约束用户代码。
2. **`assign` 从白名单移除**：模型用 `assign(v=...)` 造临时列会被"幻觉字段"校验误拦；改为禁止造列、直接对聚合列计算，并给执行加一次带反馈重试。03 因此从 fallback 转为 llm 通过。

## 9. P3 加固（复核后）

- `GroupBy.apply` 的字符串参数改为**白名单聚合名**（`sum/mean/count/size/min/max/median/std/nunique`），
  下划线名与非白名单名（如 `__class__`、`os`）一律静态拒绝；补 `apply('__class__')` 测试。
- 清理 `sandbox.py` 死分支（未使用的 `_ROOT_NAMES`、groupby 空分支）。
- 全量 227 tests OK。

## 10. 未解决 / 已知风险

- 真实 2018 语义 JSON 不闭合仍未根治（本轮降级一次），执行链路对它有效；
- `memory_guard` 在无 Job 权限的环境会 fail-closed（拒绝执行并降级），未做 fallback 放行；
- `dist/` 免安装包内起 worker 的冒烟未做（需在打包环境手工验证）；
- 对账只覆盖 total + 主导维度分组，复杂多指标计划未纳入对账范围。
