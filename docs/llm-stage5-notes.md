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
