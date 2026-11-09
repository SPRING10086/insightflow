# LLM 阶段 5 交付记录

## 5a-1 统一配置（2026-11-09）

### 实现

- `config.py` 新增 `LLMSettings`：四个功能开关、总开关、预算（单表/整批）、请求/沙箱超时、
  沙箱内存与输入行数、结构化输出/传输重试次数、长 prompt 预警阈值。
- `Settings.llm` 为唯一事实源；`load_settings()` 从 `config.json` 的 `llm` 块解析。
- **默认全关**：`enabled=false`；`enabled=true` 时按 `features` 分别控制（真值表有单测）。
- **fail-closed**：未知键、类型错误、越界（预算 0–100、超时 1–600、内存 64–4096、重试 0–3、
  单表预算不得大于整批）一律 `ConfigError`，启动失败、不静默取默认。
- **CLI 覆盖语义**：`--semantic/--plan/--execute/--narrative` 缺省为 `None`（读配置）；
  显式传 `none` 关闭、传 `llm` 开启；覆盖只影响本次运行，不写回配置。
- 预算/超时/内存/重试从配置注入 provider 构建：语义/计划/叙事使用整批预算，
  执行额外使用沙箱超时与内存，请求超时与传输重试统一覆盖。

### 测试（10 项，先红后绿）

`tests/test_llm_settings.py`：

- 默认全关、真值表（总开关关时任何 feature 都不生效）、未知 feature 报错；
- 18 组非法配置逐项 fail-closed；
- 配置缺省/部分合并；
- 非法配置时 CLI 退出 1 且不产出报告；
- CLI 覆盖不落盘（配置文件字节不变）；
- 配置开启但无密钥时走降级（有告警）；显式 `--semantic none` 覆盖后不尝试。

### 验证

```text
Ran 260 tests  OK
```

### 已知边界

- 密钥仍只来自环境变量/`.env`，不进入 `config.json`；GUI 只显示已配置状态（5b）。
- 请求超时/传输重试的重试策略细节在 5a-3 统一并补统计。

## 5a-2 打包冒烟（2026-11-09）

### 构建

- `scripts/build_portable.ps1 -SkipSmoke` 真实构建 `dist/InsightFlow`：**198 MB**，包内解释器 3.10.4、pandas 2.3.3 / numpy 2.2.6。
- 冒烟用**包内解释器**运行：`dist\InsightFlow\python\python.exe scripts\smoke_portable_llm.py`。

### 关键发现并修复：分发包曾携带 `.env`

首次构建后检查发现 `dist/app/.env`（732 字节，含真实 API Key）被打进便携包。
- 修复：构建脚本 `/XF` 增加 `.env`、`.env.*`；
- 冒烟新增两项硬断言：`no_dotenv_in_bundle`、`no_api_key_in_bundle`（扫描 app 内文本文件中的 `sk-` 长串）；
- 重建后两项均 PASS，`dist/app` 下只剩 `.env.example`。

### 冒烟检查（17 项，全 PASS，41.5 秒）

| 类别 | 检查 |
|---|---|
| 解释器 | 包内解释器存在、`sys.executable` 指向包内、Python 3.10.4 |
| 秘密 | 无 `.env`、无 `sk-` 密钥串 |
| 来源 | `insightflow` 从 `dist/app/src` 导入；`sys.path` 不含仓库 `src` |
| 依赖 | `requirements.lock.txt` 存在，pandas/numpy/openpyxl/xlrd 版本逐项一致 |
| worker | 起进程、最小聚合 total=600、分组正确、**Job Object 可用** |
| 四开关 | `--semantic/--plan/--execute/--narrative llm` 无密钥均安全降级、exit=0、报告生成、无占位符残留 |
| 配置驱动 | 配置文件开启 semantic（不带 CLI 开关）同样触发并降级 |

**干净环境**：每个子进程只注入 `SystemRoot/TEMP/TMP/USERPROFILE` + 最小 `PATH`，并显式清空
`PYTHONPATH/PYTHONHOME/PYTHONSTARTUP`；`PYTHONPATH` 仅指向包内 `app/src`，`PYTHONHOME` 指向包内 `python`。

原始证据：[docs/llm-stage5-smoke.json](llm-stage5-smoke.json)。

### 验证

```text
Ran 262 tests  OK        （260 + 2 打包防泄漏回归）
```

### 未解决

- 分发包**未签名**，也**未在干净虚拟机**上验证（checklist 与 `verify_release.ps1` 在 5a 收尾交付）。
- 冒烟覆盖无密钥降级路径；有密钥的真实调用另在有 `.env` 的环境抽测，密钥不进分发包。

## 5a-3 JSON 稳定性与统计（2026-11-09）

### 统一重试与预算阻断

- 结构化输出失败（JSON 不闭合/契约不符）按 `llm.retry.structured_output`（默认额外 2 次）重试，附上一次错误与格式要求；
- **重试前先查预算**：`剩余预算 = batch_cny - 已花 - 本次调用成本`；若 `剩余 <= 0` 或 `预估重试成本 > 剩余`，
  记为 `retry_blocked` 并立即降级，**不再发起会烧钱的重试**；
- 语义、计划、执行、叙事四条链路全部接入同一策略；传输层 429/超时重试仍独立（`retry.transport`）。

### 统计项（核心中立模块 `insightflow/stats.py`，llm 侧 re-export）

`calls / retries / json_failures / fallbacks / retry_blocked / cost_cny / max_prompt_tokens / prompt_warn`，
写入 `run_record.metadata.llm_stats`；四段旁路任一为 fallback 时 `fallbacks` 递增，叙事失败也计入。

- `scripts/llm_stats.py` 跨运行汇总 `output/*/run_record.json`：
  `runs / runs_with_llm / calls / json_failures / retries / fallbacks / retry_blocked / cost / json_failure_rate / fallback_rate`；
  无 LLM 的运行计入 runs 但贡献零，**0 次运行也有确定输出**。

### 长文本成本预警

单次 `prompt_tokens > llm.warn_prompt_tokens`（默认 8000）时记录 `prompt_warn=true` 并 `logger.warning`，
继续执行但不放宽预算护栏。

### 测试（6 项，先红后绿）

- 语义链路：坏 JSON 重试到上限 → `calls=3 / json_failures=3 / retries=2`；
- 预算阻断：首次调用后剩余不足 → `retry_blocked=1`、仅 1 次调用、抛 `CostLimitExceeded`；
- 叙事链路：坏 JSON 一次后成功 → `retries=1`、结果正常；
- 管线故障注入：语义坏 JSON → `semantic_schema` 为 fallback、`status=success`、`metadata.llm_stats` 有失败/重试/降级；
- 跨运行汇总：0 次运行与 2 次运行的聚合值。

### 验证

```text
Ran 268 tests  OK
便携包冒烟 17/17 PASS（重建 dist 208 MB）
```

### 已知边界

- 执行链路（代码生成）的 JSON 重试固定为 1 次追加（沿用原设计），未使用 `retry.structured_output` 配置；
- 长 prompt 只预警不截断；截断会改变输入语义，留待后续评估。

