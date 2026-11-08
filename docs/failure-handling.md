# 异常兼容与排障手册

## 1. 运行状态

| 状态 | 含义 | 是否生成报告 | CLI 退出码 |
|---|---|---:|---:|
| `success` | 完成读取、清洗、分析、报告 | 是 | 0 |
| `no_data` | 文件可读，但目标日期无数据 / 数据源为空 | 否 | 2 |
| `skipped` | 同指纹复用、并发锁冲突 | 否（可能复用旧报告） | 3 |
| `failed` | 没有可读数据、清洗后为空、渲染失败、配置/目录错误 | 否 | 1 |

每次运行都会写：

```text
logs/runs.jsonl
logs/runs/<run_id>.json
logs/latest_run.json
logs/insightflow.log
```

关键字段：

```json
{
  "run_id": "20261108-093000-ab12",
  "trigger": "scheduled",
  "started_at": "2026-11-08T09:30:00+08:00",
  "finished_at": "2026-11-08T09:30:06+08:00",
  "target_date": "2026-11-07",
  "files_scanned": 12,
  "files_read": 9,
  "files": [{ "relative_path": "sales.xlsx", "status": "read", "rows_target": 120 }],
  "report_generated": true,
  "report_path": "output/2026-11-07/InsightFlow_....html",
  "failure_reason": "",
  "warnings": ["[header_repaired] ..."],
  "steps": [{ "name": "读取与解析", "duration_ms": 420 }]
}
```

---

## 2. 文件级问题

| `code` | 触发条件 | 处理 | 操作建议 |
|---|---|---|---|
| `empty_file` | 0 字节 / 解码后为空 | 记录 error，继续其他文件 | 删除或替换空文件 |
| `binary_file` | 含 NUL 字节 | 拒绝按 CSV 读取 | 确认文件类型 |
| `excel_open_error` | 损坏、加密、扩展名不符 | 记录 error，继续 | 用 Excel 打开并另存 |
| `missing_xls_engine` | 缺少 xlrd | 记录 error | `pip install xlrd` |
| `no_usable_sheet` | 工作簿只有封面/空表 | 记录 error | 检查 Sheet |
| `empty_sheet` | 空 Sheet | info，跳过 | 可忽略 |
| `sheet_read_error` | 单 Sheet 读取失败 | warning，继续其他 Sheet | 检查合并单元格/公式 |
| `file_too_large` | 超过 `max_file_mb` | 跳过 | 拆分文件或调大配置 |
| `rows_truncated` | 超过 `max_rows_per_file` | 截断并 warning | 拆分或调大上限 |
| `ragged_rows` | 行列数不一致 | 补/截断并 warning | 检查未转义的逗号 |
| `csv_parse_error` | CSV 解析异常 | warning，尽力读取 | 用标准 CSV 导出 |
| `unsupported_type` | 扩展名不在配置 | 跳过 | 改配置或转格式 |

---

## 3. 编码 / 表头问题

### 3.1 编码回退顺序

```text
UTF-16 BOM → UTF-8 BOM → UTF-8 → GB18030 → GBK → Big5 → CP1252
```

- UTF-8 优先，GB18030 作为中文主力回退；
- 解析结果按可打印字符、CJK 比例、替换字符、控制字符打分；
- 最终编码写入 `FileReport.encoding` 和报告的数据血缘表；
- 无法解码时用 UTF-8 + replacement，并记录 `encoding_fallback`。

### 3.2 乱码修复

- 源文件行号使用物理行号（CSV 的 `reader.line_num` / Excel 的 Sheet 行），日期筛选和清洗后仍可回溯 `__source_row`；
- 支持 UTF-8 被误读为 GBK / Latin-1 / CP1252 的 round-trip；
- 只在文本“疑似乱码”或“无 CJK 且修复后出现 CJK”时触发，避免破坏正常中文；
- 表头修复计入 `header_repaired` warning，并汇总到质量区的“乱码/异常文本修复”；
- 有损替换字符（`�`）无法保证恢复，保留并在兼容性提示中说明。

### 3.3 表头识别

- 扫描前 `header_scan_rows` 行；
- 评分：语义别名命中、文本比例、唯一性、后续行列数一致性、首行加分、数字比例惩罚；
- 支持封面行、说明行、空行、标题行在前的情况；
- 若仍识别错误，报告会显示字段映射，便于人工核对；可调整 `header_scan_rows`。

---

## 4. 字段 / 日期问题

| `code` | 触发 | 处理 |
|---|---|---|
| `no_date_column` | 没有可用日期字段 | 跳过该表，记录 error |
| `date_parse_low` | 日期字段解析率 < 15% | 保留为候选，记录 warning |
| `no_parseable_dates` | 所有候选都解析失败 | 跳过该表 |
| `date_values_unparsed` | 部分日期无法解析 | 跳过这些行，记录数量 |
| `target_date_absent` | 文件有效但无目标日 | info；若全部文件都无目标日 → `no_data` |
| `no_metric_column` | 没有数值指标 | 降级为记录频次/质量分析 |
| `no_dimension_column` | 没有分类维度 | 只做 KPI / 质量分析 |
| `duplicate_semantic_column` | 两列映射到同一语义 | 第二列重命名保留，记录 info |
| `metric_parse_low` | 指标列可解析率 < 50% | 保留语义，warning |

### 常见原因

- 日期列是“2026-11”月粒度：当前按日分析会视为不可解析；请导出日粒度。
- 日期列是文本但格式特殊：可在 `utils.parse_date_value` 增加格式，并补测试。
- **日月歧义**：`03/04/2026` 这种两位日/月都 ≤12 的值默认按 **日/月/年** 解析为 2026-03-04。否则无法在不看数据上下文的情况下判断；跨区域数据建议直接使用 `YYYY-MM-DD`。
- 金额列带“元/万/%”：`parse_number` 已处理；若还有特殊单位，扩展该函数。
- **率/占比/负向字段**：`毛利率`、`利润率`、`销售额占比`、`非销售额` 不会被当作金额求 SUM；它们会降级为普通未知指标，避免污染收入。需要精确分析时建议改名为明确的金额列或补充自定义口径。
- 字段名不在别名表：会被识别为未知数值指标或低基数维度；扩展 `schema.FIELD_META/ALIASES`。

---

## 5. 清洗问题

| 规则 | 行为 | 可审计位置 |
|---|---|---|
| 完全重复行 | 按业务列去重，保留首次 | 质量区“完全重复去重” |
| 数值缺失 | 当期非缺失中位数；无中位数填 0 | 字段质量“填补数” |
| 文本缺失 | `未填写` | 字段质量 |
| ID 缺失 | `未提供` | 字段质量 |
| IQR 异常 | 只标记 `__outlier`，不删除 | 字段质量 / 异常清单 |
| 负值 | 保留，标记 `__negative` | 风险摘要 |
| 乱码单元格 | 尝试修复，计数 | 质量区“乱码/异常文本修复” |
| 全部为空的行 | 丢弃，计数 | 清洗报告 `dropped_all_empty` |

质量分是**启发式**，不是统计认证：

```text
60% 字段完整度
+ 15% 重复控制
+ 15% 异常控制
+ 10% 解析质量
```

---

## 6. 调度 / 并发问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `status=skipped`，reason 含“并发” | 另一个 run 持有 `.pipeline.lock` | 等待；确认无僵尸进程后可删除锁 |
| 锁一直存在 | 进程被杀 | 默认 150 分钟后自动过期；可手动删除 `state/.pipeline.lock` |
| `status=skipped`，reason 含“指纹” | 源文件未变化 | 正常；需要重算用 `--force` |
| 计划任务没有跑 | 任务未安装/用户未登录/路径变化 | `schedule status`，检查 `logs/scheduler_console.log` |
| 09:30 没跑但机器 10:00 才开 | Task Scheduler `StartWhenAvailable` 未触发 | 检查任务设置；daemon 在 `grace_minutes`（默认 180 分钟）内会补跑 |
| daemon 触发失败 | 网络盘/文件暂时不可用 | 失败不标记当天完成，5 分钟后在宽限期内自动重试 |
| 任务执行但报告目录无文件 | 工作目录/权限/`PYTHONPATH` | 查看 `logs/insightflow.log` 与 `scheduler_console.log` |

Windows 任务使用当前用户 `InteractiveToken`，通常不需要管理员权限；若 `schtasks` 返回拒绝访问，请用管理员 PowerShell 执行 `schedule install`。

---

## 7. 报告问题

| 现象 | 排查 |
|---|---|
| 图表空白 | 确认浏览器启用 JavaScript；检查是否被企业策略禁用；表格仍可读 |
| 报告打不开 | 确认是完整 `.html` 文件，不是 `.tmp`；查看同目录 `run_record.json` |
| 数字和表格不一致 | 不应发生；图表与表格来自同一 `ChartSpec.data`，如出现请提交 issue 附 run_id |
| 打印分页不理想 | 使用 Chrome/Edge 打印；已设置卡片 `break-inside: avoid`，可在打印预览调整缩放 |
| 报告里没有某个图 | 看“分析计划”中的跳过原因，或数据不足 |
| 报告没有环比 | 前一日无数据 / `comparison.enabled=false` |

---

## 8. 快速排障命令

```powershell
# 只扫描，不生成
.\run.ps1 scan --date 2026-11-07

# 看最近运行状态 / 失败原因
.\run.ps1 logs --limit 10

# 忽略指纹强制重算
.\run.ps1 run --date 2026-11-07 --force

# 看计划、指标、清洗明细
Get-Content output\2026-11-07\analysis_plan.json
Get-Content output\2026-11-07\metrics.json
Get-Content output\2026-11-07\cleaned_data.csv -TotalCount 5

# 看计划任务
.\run.ps1 schedule status
```

---

## 9. 设计原则

1. **一个文件坏，不代表一批数据坏**：文件级错误被降级为 issue。
2. **没有数据不等于失败**：`no_data` 是正常业务状态，退出码区分。
3. **不猜日期、不猜单位、不猜因果**：所有推断都写入计划/报告。
4. **异常值保留**：只标记，交给人判断。
5. **日志先于报告**：即使报告渲染失败，运行记录也必须落盘。
