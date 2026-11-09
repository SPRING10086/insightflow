# LLM 阶段 0 · 方向验证 PoC 评估报告

> 依据：《InsightFlow LLM 阶段 0 PoC 任务书》
> 结论先行：**真实调用已完成，预注册六项 Go/No-Go 全部达成，判定由 Conditional Go 升级为 Go。**

## 0. 实测档头（真实调用，2026-11-09）

| 项 | 实测值 |
|---|---|
| 模型标识（API 名） | `deepseek-flash`（服务端实际模型 DeepSeek-V4.1-Flash-1017） |
| Prompt 版本 | `analysis_plan_v1` |
| 计费单价（人民币/百万 tokens，按高峰保守填） | 输入（缓存未命中）2 元、输出 8 元；闲时官方为半价，实际成本只会更低 |
| 端点 | `https://api.deepseek.com/chat/completions`（OpenAI 兼容，非思考模式） |
| 真实调用 | 2 次：主样本、边界样本，均首次即成功 |
| 两次合计 | 3791 tokens，成本 0.0165 元 |

---

## 1. 交付物

```text
src/insightflow/llm/
├─ payloads.py      # 载荷最小化 + 脱敏 + 行数断言
├─ contracts.py     # 版本化输出契约（拒绝业务数字、拒绝非空数据槽）
├─ client.py        # 标准库 httpx 替代：urllib；重试/超时/成本熔断
├─ evaluate.py      # 角色准确率、编造数字检测、汇总
├─ prompts/analysis_plan_v1.md
└─ __main__.py      # python -m insightflow.llm payload|plan
tests/test_llm_poc.py   # 23 项 mock 测试，不依赖真实 API
.env.example            # 配置模板（.env 已 gitignore）
```

核心零改动；`llm/` 与主流程完全隔离（有测试断言：核心不得 import `insightflow.llm`）。

## 2. 上云载荷：最小化与脱敏（已机械验证）

`python -m insightflow.llm payload <csv> --out logs/llm_payload.json`

| 检查 | 实测 |
|---|---|
| 只上传字段结构 + **前 10 行** | `row_count_exposed = 10`；第 11 行哨兵值不在载荷中（测试断言） |
| 手机号 | `13812345678` → `138****5678` |
| 邮箱 | `zhangsan@example.com` → `zh***@example.com` |
| 人名（字段名含「姓名/联系人…」） | `张三` → `张*` |
| 公司（按字段名或 `有限公司` 后缀识别） | `华为技术有限公司` → `华为**有限公司`（保留形态与后缀） |
| 地址（字段名含「地址/住址…」） | `深圳市南山区科技园1号` → `深圳*********` |
| 原始值泄漏检查 | 手机/邮箱/公司原始值在序列化载荷中均未出现 |

> 脱敏只做"形态保留"，不保证不可逆（公司名可能被猜出）；因此载荷仍是"结构性数据"，按任务书边界不上传完整明细。

## 3. 结构化输出契约（拒绝不可信输出）

`contracts.parse_result()` 强制：

- 顶层必须是 `semantic_schema` + `analysis_plan`；
- 字段 `role ∈ {date, dimension, metric, id, text}`，`confidence ∈ [0,1]`；
- **任何 metric 类键（value/total/sum/amount/result/kpi/revenue/orders/cost/profit/margin/count）带数字 → 直接失败**；
- 图表 `xAxis.data / yAxis.data / series[].data` 必须为空（数据槽位留给本地引擎）；
- 非 JSON、缺字段、非法 role → 失败，不做"自由文本兜底"。

对应测试 7 项，全绿。

## 4. 成本护栏与请求健壮性

| 机制 | 行为 | 实测（mock） |
|---|---|---|
| 成本计量 | 按 tokens × 单价（人民币/百万 tokens）实时累计 | 1000/500 tokens @1000 元/M → 1.5 元 |
| 预警/硬停 | 4.5 元预警、5.0 元硬停（`CostLimitExceeded`） | 高价配置下单次即触发硬停 |
| 调用前检查 | 已超阈值则不再发请求 | 单测覆盖 |
| 限流/5xx/网络 | 指数退避重试（默认 2 次） | 429→200 成功（2 次调用） |
| 鉴权失败 | 不重试，直接失败 | 401 只调用 1 次 |
| 密钥/单价 | 全部来自环境变量或 `.env`，**代码不内置默认模型名与单价** | `grep` 无硬编码模型字符串 |

配置缺失时的行为（真实执行，非模拟）：

```text
$ python -m insightflow.llm plan examples/acceptance/01_主样本_干净.csv
配置错误：缺少配置：INSIGHTFLOW_LLM_MODEL、DEEPSEEK_API_KEY、INSIGHTFLOW_LLM_PRICE_IN、INSIGHTFLOW_LLM_PRICE_OUT。
请在环境变量或本地 .env 中填写；模型标识必须使用实测可用的 API 名称，单价按官方定价填写，代码不内置默认单价。
exit=2
```

## 5. 真实调用实测结果（2026-11-09 补跑）

补跑前先修复两个只有真实调用才会暴露的问题：

1. **端点路径缺陷**：`client.py` 的 `_endpoint()` 仅返回 base_url、未拼接 `CHAT_PATH`，请求会打到根路径；已改为 `base_url + /chat/completions`（mock 测试因替换 transport 无法发现）。
2. **默认思考模式**：Flash 默认开启思考会额外产生推理 token；结构化规划无需推理，已在请求体显式 `"thinking": {"type": "disabled"}` 以控成本与延迟。

| 指标 | 主样本（干净） | 边界样本（含问题） |
|---|---|---|
| 首次解析 | 成功 | 成功 |
| latency_ms | 4506 | 4328 |
| prompt / completion tokens | 1247 / 830 | 1053 / 661 |
| 成本（元，高峰单价） | 0.0091 | 0.0074 |
| 字段角色准确率 | 5/5 = 100% | 5/5 = 100% |
| 编造业务数字 | 0 | 0 |

边界样本质量点识别（人工预埋 5 项，命中 4 项）：日期格式不一致、销售额空值、订单号重复、毛利率异常高值均命中；销售额 50000 离群点未被单独点名（次要召回遗漏，不影响字段角色判定）。

> 历史排障记录：`curl` 报 `schannel: SEC_E_NO_CREDENTIALS` 是 Windows 证书栈问题，不代表断网；Python `urllib` 可正常握手，本次真实调用即由其完成。

## 6. 补测步骤（已执行，可复现）

以下步骤已于 2026-11-09 全部执行，原始结果落盘在 `logs/run_main.log`、`logs/run_edge.log`：

```powershell
# 1) 配置（.env 已创建：模型 deepseek-flash、Key、高峰单价 2/8）
copy .env.example .env

# 2) 先看载荷（不联网）——已确认两表均只暴露前 10 行且策略标记 masked
$env:PYTHONPATH="src"; python -m insightflow.llm plan examples/acceptance/01_主样本_干净.csv --dry-run

# 3) 真实调用（逐表）——均 exit=0
python -m insightflow.llm plan examples/acceptance/01_主样本_干净.csv
python -m insightflow.llm plan examples/acceptance/02_边界样本_含问题.csv
```

每次输出包含 `latency_ms`、`usage`、`cost`、字段角色、图表骨架，正是报告所需指标。`examples/raw/` 下其余表的批量调用留待阶段 1。

## 7. 预注册的 Go / No-Go 判定（避免事后凑数）

| 维度 | Go | No-Go | 实测（2026-11-09） |
|---|---|---|---|
| 结构化输出可解析率 | **100%**（2 次尝试内） | <100% | **100%**，2/2 首次即解析 |
| 字段角色准确率（对照现有规则 + 人工标注） | **≥85%** | <70% | **100%**（10/10 字段角色全对） |
| 单表成本 | **<5 元**（硬停有效） | ≥5 元或熔断失效 | **0.0091 / 0.0074 元**，未触预警 |
| 编造业务数字 | **0 次** | ≥1 次 | **0 次** |
| 延迟 | P95 ≤ 30 s | P95 > 60 s | **4.5 / 4.3 s** |
| 限流/超时恢复 | 重试后成功 | 重试仍失败 | 未触发限流、单次成功（重试机制由单测覆盖） |

**最终判定：Go。** 六项预注册标准全部达成；架构、载荷、契约、成本护栏与隔离性另由 23 项 mock 测试 + 真实 CLI 证明。

## 8. 风险与阶段 1 建议

1. **脱敏非不可逆**：公司/地址掩码仍可能被推断；阶段 1 若扩大样本量，建议改为"只传列名 + 统计特征（非样本值）"，或提供本地脱敏预览二次确认。
2. **成本随列数/样本线性增长**：单表 5 元门槛在校验阶段够用；多表批量需在阶段 1 引入总预算（如整批 ≤20 元）。
3. **模型标识漂移**：必须把"实测模型名 + 单价 + prompt 版本"写进报告的 header，并在 CI 里做契约回归（无需真实调用）。
4. **接入方式**：阶段 1 建议"LLM 出计划 → 本地规则引擎校验/执行 → 数字仍由 execute() 产出"，绝不让模型直接产出业务数字。

## 9. 测试

```text
$env:PYTHONPATH="src"; python -m unittest tests.test_llm_poc -v
Ran 23 tests  OK

$env:PYTHONPATH="src"; python -m unittest discover -s tests
Ran 163 tests ... FAILED (failures=1, skipped=14)
```

唯一失败 `test_gui_shell.EnvironmentTests.test_environment_check_matches_lock` 是本机环境漂移：当前解释器为 pandas 3.1.0 / numpy 2.5.4，而 `requirements.lock.txt` 锁定 2.3.3 / 2.2.6；与本次 LLM 改动无关，LLM 模块仅用标准库。若需全绿，可按 `python -m pip install -r requirements.lock.txt` 对齐锁定环境（会改动本机依赖，未擅自执行）。

新增的隔离断言：核心与界面代码不得 `import insightflow.llm`；`src/insightflow/llm/client.py` 是唯一允许使用 `urllib` 的文件。
