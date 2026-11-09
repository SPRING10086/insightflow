# LLM 阶段 5 工程化集成 · 设计（待审查方确认后实现）

> 基线 `59c143d`（250 tests、86.4%）。四个 LLM 能力仍是旁路、默认全关。
> 本阶段只做"可靠性 + 可用性"，不让 LLM 驱动报告；任何配置/界面都不得绕过沙箱、对账、占位符、转义、脱敏与成本护栏。

## 1. 子阶段拆分与顺序

| 子阶段 | 内容 | 放行门槛 |
|---|---|---|
| **5a-1 统一配置**（地基） | `Settings.llm` 配置块、校验、CLI/GUI 共用 | 默认全关 + 非法值 fail-closed 单测 |
| **5a-2 打包冒烟** | 干净环境（清空 PYTHONPATH/PYTHONHOME）下用包内解释器验证 worker 与四开关 | 审查方独立复跑冒烟脚本 |
| **5a-3 JSON 稳定性** | 结构化输出重试、失败/重试/降级统计、长文本成本预警 | 注入不闭合 JSON 故障，重试→降级、status=success、成本不破护栏 |
| **5b-1 GUI 集成** | 四个开关 + 成本/来源/对账/降级展示 | 默认关闭时界面与现状一致；构建产物回归 |
| **5b-2 运维文档** | 开关含义、预算、降级、排查、配置清单 | 文档评审 |
| 发布前置 | 代码签名 + 干净 VM 清单与验证脚本 | 资源到位后执行，正式分发硬门禁 |

## 2. 配置 schema（`config.json` 新增 `llm` 块）

```json
{
  "llm": {
    "enabled": false,
    "features": {"semantic": false, "plan": false, "execute": false, "narrative": false},
    "budget": {"per_table_cny": 5.0, "batch_cny": 20.0},
    "timeout": {"request_seconds": 60, "sandbox_seconds": 10},
    "sandbox": {"memory_mb": 512, "max_input_rows": 200000},
    "retry": {"structured_output": 2, "transport": 2},
    "warn_prompt_tokens": 8000
  }
}
```

规则：

1. **默认全关**：`llm.enabled=false` 或任一 feature=false → 对应 provider 不构建，零调用。
2. **fail-closed 校验**：类型错误、负数、越界（budget 超出 `[0, 100]`、timeout 超出 `[1, 600]`、memory 超出 `[64, 4096]`、retry 超出 `[0, 3]`、未知 feature 键）→ `ConfigError`，启动失败并给出可读消息，不静默取默认。
3. **单一来源**：`Settings.llm` 是唯一事实源；CLI `--semantic/--plan/--execute/--narrative` 只做**单次覆盖**（CLI > config > 默认），GUI 读写同一 `config.json`，不存在第二套口径。
4. **密钥不进 config.json**：API Key/模型名仍只来自环境变量或 `.env`；GUI 只显示"已配置/未配置"状态，不落盘、不回显。
5. **越界即关**：配置非法时整个 LLM 旁路关闭（而不是部分开启），主流程照常。

## 3. 干净环境打包冒烟（发布硬门槛）

脚本 `scripts/smoke_portable_llm.py`（由**包内解释器** `dist/InsightFlow/python/python.exe` 执行），流程：

1. **环境清理**：子进程 env 仅保留 `SystemRoot/TEMP/TMP/USERPROFILE/PATH(系统最小)`，显式清空 `PYTHONPATH`、`PYTHONHOME`、`PYTHONSTARTUP`。
2. **解释器自证**：`sys.executable` 必须位于 `dist/InsightFlow/python/`；打印 `sys.version`（要求 3.10.x）。
3. **依赖对齐**：`pandas/numpy/openpyxl/xlrd` 版本与 `requirements.lock.txt` 逐项比对，不一致即失败。
4. **worker 冒烟**：用 `sandbox.run_sandbox` 起 worker，跑最小聚合 `{"total": sum(revenue)}`，要求 `memory_guard=job`、结果与本地计算一致。
5. **四开关冒烟**：用包内 CLI 对一个小 CSV 依次执行 `--semantic llm / --plan llm / --execute llm / --narrative llm`（无密钥环境），断言：
   - 每次 `status=success`；
   - 每次在 `metadata` 留下 `fallback` 原因；
   - 报告 HTML 正常生成、无 `[[` 残留。
6. **无外部依赖**：断言 `sys.path` 不含项目 `src`（仅包内路径）、无 `site-packages` 之外的第三方包。

输出：`logs/smoke_portable_llm.json`（逐项 PASS/FAIL + 版本 + 耗时），失败退出码非 0。

> 构建仍用 `scripts/build_portable.ps1`；冒烟脚本独立，可在已有 dist 上反复跑，审查方无需重构建即可复跑。

## 4. JSON 稳定性与统计

### 4.1 重试策略（统一）

| 场景 | 重试 | 反馈 | 上限 |
|---|---|---|---|
| 结构化输出 JSON 不闭合/契约失败 | 额外 2 次（可配） | 附上次错误 + 格式要求 | `retry.structured_output` |
| 429/5xx/网络 | 2 次退避（现有） | 无 | `retry.transport` |
| 字段漏/多 | 1 次（现有） | 逐字字段要求 | 固定 |

每次重试前检查预算：`spent + 预估 >= batch_cny` 则**不再重试**，直接降级，保证"重试不把成本顶破护栏"。

### 4.2 统计项（写入 `run_record.metadata.llm_stats`）

`calls`、`json_failures`、`retries`、`fallbacks`、`cost_cny`、`max_prompt_tokens`、`prompt_warn`。跨运行汇总脚本 `scripts/llm_stats.py` 读取 `output/*/run_record.json`，输出近 N 次的不闭合率、平均重试、降级率、成本分布。

### 4.3 长文本成本预警

单次 `prompt_tokens > warn_prompt_tokens` 时：`logger.warning` + `llm_stats.prompt_warn=True`，继续执行但计入统计；预算护栏不变。

## 5. 降级矩阵（新增项加粗）

| 触发 | 行为 | 留痕 |
|---|---|---|
| 配置缺失/非法 | **LLM 全关，主流程照常** | `ConfigError` 或启动告警 |
| JSON 不闭合/契约失败 | 重试 N 次 → 仍失败则降级 | `json_failures/retries/fallbacks` |
| **重试将超预算** | 立即降级，不再重试 | `reason=budget_retry_blocked` |
| 429/超时/网络 | 现有退避重试 → 降级 | `reason=request` |
| 沙箱 AST/超时/内存/输出 | 现有逐级降级 | `reason=sandbox` |
| 对账失败 | 不采信 LLM 数字 | `reason=reconcile` |
| 叙事裸数字/未知引用/XSS | 回退规则洞察 | `reason=narrative_*` |
| **长 prompt** | 继续执行 + 预警 | `prompt_warn=true` |

## 6. GUI 改动点（5b）

- 新增「LLM 增强（实验）」面板：四个开关（默认关）+ 预算显示（只读）+ 密钥状态（已配置/未配置）。
- 保存：写回 `config.json` 的 `llm` 块（经 `gui/services.py`，GUI 不直接读写文件、不 import llm）。
- 运行结果区新增一行：`LLM 成本 ¥x.xx · 语义 来源 · 计划 来源 · 执行 对账通过/未通过 · 叙事 正常/降级(原因)`；
- 文案口语化：如"本次未启用 LLM 增强"、"已自动回退到规则结果（原因：模型输出格式异常）"。
- 默认关闭时：界面与现状一致，不新增可见区块（回归截图对比）。
- 隔离：GUI 仍只经 `services.py` 调核心；装配点仍是 `semantic_assembly.py`。

## 7. 发布前置：checklist 与验证脚本

`docs/release-checklist-llm.md` + `scripts/verify_release.ps1`（可执行）：

1. **签名**：`signtool sign /fd SHA256 /tr <timestamp> /td SHA256 <exe/vbs/bat>`（列出需签名文件清单：`启动操作端.vbs`、`启动操作端(排障).bat`、`run.bat`、`python.exe`、`InsightFlow*.dll`）。
2. **干净 VM**：全新 Windows 10/11 → 安装 VC++ 2015-2022 Redistributable → 解压 dist → 双击 VBS → 确认窗口打开、无命令黑框、日志生成。
3. **功能冒烟**：用验收样本跑一次默认关闭报告 + 一次四开关全开（有密钥）/全关（无密钥）降级。
4. **网络**：默认关闭时应无出站连接；开启 LLM 时仅访问配置的 API 域名。
5. **验证脚本**：`verify_release.ps1` 自动检查文件哈希、版本号、签名状态（`Get-AuthenticodeSignature`）、dist 冒烟结果，输出 PASS/FAIL 表。

## 8. 测试计划

| 层 | 用例 |
|---|---|
| 配置 | 默认全关；非法类型/越界/未知键 fail-closed；CLI 覆盖不落盘；GUI 与 CLI 读同一配置 |
| 打包 | 冒烟脚本四项（解释器/版本/worker/四开关）；无外部 PYTHONPATH |
| 稳定性 | 注入不闭合 JSON：重试 N 次 → 降级、统计正确、status=success；重试超预算即停 |
| 统计 | 跨运行汇总脚本口径正确（含 0 次运行） |
| GUI | 默认关闭界面不变；开关→config 落盘；结果区成本/对账/降级展示；重建产物回归 |
| 文档 | 运维说明覆盖四开关、预算、降级、排查 |
| 门禁 | Python310 全量、覆盖 ≥85%、dist 冒烟 PASS |

## 9. 待确认点

1. **密钥入口**：GUI 只显示"已配置/未配置"（不提供输入框，避免把 Key 写进 config），是否接受？
2. **CLI 覆盖语义**：`--semantic llm` 等只覆盖本次运行、不写回 config，是否认可？
3. **冒烟范围**：是否要求 dist 冒烟同时验证"有密钥真实调用"？（默认用无密钥降级冒烟，真实调用另跑，避免把密钥放进分发包）
4. **预算展示**：GUI 只读展示"单表 5 元 / 整批 20 元"上限与本次已用，是否需要可编辑？
