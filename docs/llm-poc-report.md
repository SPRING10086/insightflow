# LLM 阶段 0 · 方向验证 PoC 评估报告

> 依据：《InsightFlow LLM 阶段 0 PoC 任务书》
> 结论先行：**技术路径已机械验证可行（Conditional Go）；真实计费与准确率因本机缺少 API Key 未能实测，需在具备密钥的机器上补跑一次即可定档。**

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
tests/test_llm_poc.py   # 21 项 mock 测试，不依赖真实 API
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

## 5. 真实调用为什么还没跑

| 项 | 状态 |
|---|---|
| 网络可达性 | **已验证可达**：Python `urllib` 访问 `https://api.deepseek.com/v1/models` 返回 **HTTP 401**（能握手，仅缺鉴权） |
| API Key | **本机没有**（`DEEPSEEK_API_KEY` / `INSIGHTFLOW_LLM_API_KEY` 均为空，无 `.env`） |
| 模型标识 | 任务书要求"实测确认"，因此**没有写入任何默认模型串**；需在有密钥环境用 `/v1/models` 核对后填入 |
| 单价 | 同理不臆造，需按官方定价填入 `INSIGHTFLOW_LLM_PRICE_IN/OUT` |

> 顺带排除一个假故障：`curl` 报 `schannel: SEC_E_NO_CREDENTIALS` 是 Windows 证书栈问题，**不代表断网**；Python 能正常握手。

## 6. 补测步骤（拿到密钥后 10 分钟）

```powershell
# 1) 配置
copy .env.example .env    # 填写模型名、Key、单价

# 2) 先看载荷（不联网）
$env:PYTHONPATH="src"; python -m insightflow.llm plan examples/acceptance/01_主样本_干净.csv --dry-run

# 3) 真实调用（逐表）
python -m insightflow.llm plan examples/acceptance/01_主样本_干净.csv
python -m insightflow.llm plan examples/acceptance/02_边界样本_含问题.csv
# examples/raw/ 下逐个 csv/xlsx 转 csv 后同上
```

每次输出包含：`latency_ms`、`usage`、`cost`（tokens/人民币）、字段角色、图表骨架——正好是报告所需的四项指标。

## 7. 预注册的 Go / No-Go 判定（避免事后凑数）

| 维度 | Go | No-Go |
|---|---|---|
| 结构化输出可解析率 | **100%**（2 次尝试内） | <100% |
| 字段角色准确率（对照现有规则 + 人工标注） | **≥85%** | <70% |
| 单表成本 | **<5 元**（硬停有效） | ≥5 元或熔断失效 |
| 编造业务数字 | **0 次** | ≥1 次 |
| 延迟 | P95 ≤ 30 s | P95 > 60 s |
| 限流/超时恢复 | 重试后成功 | 重试仍失败 |

**当前判定：Conditional Go。** 架构、载荷、契约、成本护栏与隔离性已用 21 项 mock 测试 + 真实 CLI 证明；上表六项中"可解析率/成本/准确率/延迟/编造数字"必须用一次真实调用补齐——这是唯一剩余项，不是设计风险。

## 8. 风险与阶段 1 建议

1. **脱敏非不可逆**：公司/地址掩码仍可能被推断；阶段 1 若扩大样本量，建议改为"只传列名 + 统计特征（非样本值）"，或提供本地脱敏预览二次确认。
2. **成本随列数/样本线性增长**：单表 5 元门槛在校验阶段够用；多表批量需在阶段 1 引入总预算（如整批 ≤20 元）。
3. **模型标识漂移**：必须把"实测模型名 + 单价 + prompt 版本"写进报告的 header，并在 CI 里做契约回归（无需真实调用）。
4. **接入方式**：阶段 1 建议"LLM 出计划 → 本地规则引擎校验/执行 → 数字仍由 execute() 产出"，绝不让模型直接产出业务数字。

## 9. 测试

```text
$env:PYTHONPATH="src"; python -m unittest tests.test_llm_poc -v
Ran 21 tests  OK

$env:PYTHONPATH="src"; python -m unittest discover -s tests
Ran 161 tests  OK      （原 139 + 21 PoC + 1 隔离断言）
```

新增的隔离断言：核心与界面代码不得 `import insightflow.llm`；`src/insightflow/llm/client.py` 是唯一允许使用 `urllib` 的文件。
