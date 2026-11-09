# LLM 阶段 3 受控执行 · 设计（待审查方确认后实现）

> 目标：LLM 把阶段 2 的计划写成 pandas 代码，在本地沙箱跑出真实数字；数字由本地确定性执行产生，LLM 永不直接输出业务数值。
> 安全假设：**生成的代码是恶意的**；静态拒绝优先于运行时兜底；对不上账一律不采信。

## 1. 模块划分（核心零网络，沿用唯一装配点）

```text
src/insightflow/
├─ execution.py            # 中立协议 + 产物 + 对账（纯核心，无网络/pandas 执行）
│   ExecutionProvider(Protocol): generate(plan, schema, sample) -> CodeArtifact
│   ExecutionResult / ExecutionArtifact / reconcile() / save_execution_artifacts()
├─ semantic_assembly.py    # 唯一装配点：build_execution_provider("llm")
├─ pipeline.py             # execute(..., execution_provider=None)
└─ cli.py                  # run --execute none|llm（缺语义/计划时自动补建，沿用阶段 2）

src/insightflow/llm/
├─ codegen.py              # 调模型→代码 JSON 契约（只返回 code + 说明 + 字段引用）
├─ sandbox.py              # AST 校验 + 子进程启动/超时/内存限制 + 结果校验
├─ sandbox_worker.py       # 子进程入口：受限命名空间内 exec 代码
└─ prompts/execution_v1.md
```

产物（当日目录）：`execution_rule.json`（规则结果）、`execution_code.json`（模型代码+元数据）、`execution_llm.json`（沙箱结果+来源）、`execution_diff.json`（对账明细）；`run_record.metadata.execution` 汇总。默认 `execution_provider=None` 时零调用、零落盘。

## 2. AST 白名单（逐节点，静态拒绝）

**允许的节点**：`Module, Expr, Assign, AugAssign, BinOp, UnaryOp, BoolOp, Compare, Call, Attribute, Subscript, Slice, Name, Constant, List, Tuple, Dict, Set, IfExp, If, ListComp, DictComp, GeneratorExp`

**显式拒绝（黑名单优先于白名单）**：

| 类别 | 拒绝项 |
|---|---|
| 导入/动态执行 | `Import, ImportFrom, Exec, eval, exec, compile, __import__, globals, locals, vars, dir, getattr, setattr, delattr, input` |
| 名字/属性 | 任何 `Name.id` 或 `Attribute.attr` **以 `_` 开头**（覆盖全部 dunder：`__class__/__globals__/__subclasses__/__bases__/__mro__/__builtins__/__dict__`） |
| 语句/结构 | `FunctionDef, AsyncFunctionDef, Lambda, ClassDef, With, AsyncWith, Try, Raise, Global, Nonlocal, Delete, Yield, Await, For, While, Starred, NamedExpr, Match` |
| 调用目标 | 只允许 `Name`（白名单内建）或 `Attribute` 链，且链根必须是 `df/pd/np`；拒绝调用任意表达式 |

**允许的调用/方法**（其余全部拒绝）：

- 内建：`len, min, max, sum, round, abs, int, float, str, sorted, bool`
- `pd`：`to_numeric, to_datetime, cut, qcut, concat`
- `np`：`mean, sum, median, std, percentile, round, abs, minimum, maximum, where, isnan`
- `DataFrame/Series`：`groupby, agg, sum, mean, count, size, nunique, min, max, median, std, sort_values, head, tail, rename, reset_index, merge, assign, astype, fillna, dropna, isin, between, clip, round, abs, idxmax, nlargest, value_counts, pivot_table, crosstab, loc, iloc, columns, index, values`
- `GroupBy`：`agg, sum, mean, count, size, nunique, min, max, median, std, apply`（`apply` 仅允许传入已白名单的字符串函数名，禁止 lambda）

**命名空间**：`exec(code, {"__builtins__": {}, "pd": pd, "np": np, "df": df, ...})`，不注入 `os/sys/open/import`；`df` 为输入副本，执行后丢弃。

## 3. Windows 超时与资源限制（写死方案）

- **执行隔离**：`subprocess.Popen([sys.executable, "-m", "insightflow.llm.sandbox_worker"], stdin=PIPE, stdout=PIPE, stderr=PIPE, creationflags=CREATE_NO_WINDOW)`；输入用 pickle（本地内存/管道，不落盘），输出 JSON。
- **墙钟超时**：`communicate(timeout=10s)`；超时即 `taskkill /T /F /PID <pid>`，再 `proc.kill()` 兜底，判 `timeout` 降级。**不用线程**（Windows 无法可靠中断）。
- **内存上限**：父进程用 `ctypes` 创建 **Job Object**：`JOB_OBJECT_LIMIT_PROCESS_MEMORY = 512MB`，`AssignProcessToJobObject`；创建失败（权限/平台）时降级为"worker 自检 + 输入/输出行数上限"，并记录 `memory_guard=fallback`。
- **规模上限**（执行前静态判断）：输入 ≤ 200,000 行、≤ 50 列；输出指标 ≤ 200 个、分组行 ≤ 5,000、结果 JSON ≤ 1MB。
- **打包可行性**：`dist/InsightFlow/python/python.exe` 即 `sys.executable`，worker 通过 `-m` 启动、`PYTHONPATH` 指向 bundle 的 `app/src`；设计验收含"在 dist 布局下起一次 worker"的冒烟（不进 CI）。
- **失败语义**：超时/超内存/崩溃 → 杀进程树 → 该表/该计划降级规则结果，运行 `success`。

## 4. 对账容差（采信前提）

| 对象 | 容差/规则 |
|---|---|
| 计数、行数、分组键集合 | **完全一致**（集合缺一个即失败） |
| 金额/总量（sum） | `abs(a-b) <= max(1e-6, 1e-6*abs(b))` |
| 均值/比率（mean/ratio） | 相对误差 `<= 1e-4` |
| Top-N 排名 | 按值比较；同值允许顺序不同 |
| 类型 | 数值必须是 `int/float`，`NaN/Inf` 一律失败 |

对账复用 `scripts/reconcile.py` 的阈值口径（`1e-6`），不另起一套；结论写 `execution_diff.json` 与 `metadata.execution.reconcile`。

## 5. 降级矩阵

| 触发 | 处理 | 记录 |
|---|---|---|
| 配置缺失/生成超时/429/JSON 不闭合 | 不执行，回退规则结果 | `source_kind=fallback`, `reason` |
| AST 拒绝（import/dunder/open/exec/幻觉字段） | 不执行 | `blocked=<rule>`, 原始代码片段（截断） |
| 死循环/超时 | 杀进程树 | `reason=timeout`, `duration_ms` |
| 超内存 | Job Object 终止 | `reason=memory`, `memory_guard=job` |
| 输出非法（非 JSON/非数值/NaN/超限） | 丢弃输出 | `reason=schema` |
| 对账不一致 | 不采信 LLM 数字 | `reason=reconcile`, 差异明细 |
| 任一步异常 | 规则结果继续出报告 | 运行 `success` |

## 6. 测试计划

**A. 7 类故障注入（全部要求"拦住且不触达真实 I/O/网络"）**

| # | 注入 | 期望 |
|---|---|---|
| 1 | `import os` / `from subprocess import ...` | AST 拒绝，worker 未启动 |
| 2 | `df.__class__.__mro__` / `().__class__.__bases__` | AST 拒绝（`_` 开头名字/属性） |
| 3 | `open()` / `exec()` / `eval()` | AST 拒绝 |
| 4 | `while True: pass` / 超长 `apply` | 超时杀进程，降级 |
| 5 | 分配超大对象 | 内存限制终止，降级 |
| 6 | 输出非 JSON / NaN / 超 1MB | 输出校验拒绝 |
| 7 | 代码引用计划外字段（幻觉） | 执行前字段闭环拒绝 |

**B. 单元/集成**：AST 允许/拒绝逐条表驱动；worker 超时与 kill 无孤儿进程；默认关闭零行为；假 provider 成功写 4 份产物且 `metrics` 不变；对账失败降级；CLI `--execute llm` 无配置降级。
**C. 真实调用**：3 个验收样本 + ≥1 个真实工商抽样（只读前 300 行），登记成本/延迟/对账结论。
**D. 门禁**：Python310 全量测试（现 204，新增登记）、核心覆盖 ≥85%。

## 7. 待确认点

1. AST 是否允许 `ListComp/DictComp/GeneratorExp`（本设计允许；若要求最严可一并禁用）。
2. `DataFrame.merge/concat/pivot_table` 是否放行（本设计放行，均为纯内存变换）。
3. 超时 10s、内存 512MB、输入 20 万行是否合适（可按机器调整）。
4. 对账容差沿用 1e-6 / 1e-4 是否认可。
