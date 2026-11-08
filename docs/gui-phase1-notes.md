# 阶段 1 交付记录

> 依据：《InsightFlow 操作端前端要求书》《操作端分阶段开发任务书》
> 阶段 1 目标：无编程基础者能独立跑通「放数据 → 运行 → 取报告」，结果与命令行完全一致。
> 本文件按步骤追加记录；当前进度：**第 0 步（纯重构）已完成**。

---

## 第 0 步：把 `cli._cmd_scan()` 抽成 `services.preview()`（纯重构，无行为变化）

评审要求：先做纯重构，CLI 只负责打印，并配「重构前后输出逐字节一致」的回归测试；确认没有复制逻辑后再做界面。

### 1.1 做法

1. **先固化黄金样本，再动代码**。改动前用进程内捕获（`contextlib.redirect_stdout`）把 `scan --date 2026-11-07` 的文本输出与 `--json` 输出各存一份，运行两次确认稳定（`text-stable=True / json-stable=True`），并把本机绝对路径归一化为 `<ROOT>` 后作为夹具：
   - `tests/data/scan_text_golden.txt`
   - `tests/data/scan_json_golden.json`
2. 新增 `src/insightflow/services.py`：`preview(settings, target, source_dir=None) -> PreviewPayload`，把「扫描 → 读取 → 逐表识别字段/日期 → 统计目标日与基准日命中」整段搬进来；**不写报告、不取锁、不碰幂等索引**。
3. `cli._cmd_scan()` 缩减为：解析参数 → 调 `services.preview()` → 打印；表格与提示的排版移到 `_print_preview(payload)`，只读 payload，不再接触 `read_result`。

### 1.2 一个需要说明的设计选择：`PreviewPayload` 是 dict 子类

`preview()` 返回 `PreviewPayload(dict)`：

- **键**与重构前的 payload 完全一致（`target_date / baseline_date / source_dir / files_scanned / files_read / tables / file_errors`），所以 `scan --json` 一个字节都没变；
- **`warnings` 是属性而不是键**，只供文本输出与后续 GUI 使用。若把 warnings 塞进字典，`--json` 的输出就会多一个字段，直接违反「逐字节一致」的契约；
- 附带两个只读属性 `empty_source` / `has_target_rows`，把原先散落在 CLI 里的两个判断收进服务层，GUI 阶段 1 可直接复用。

### 1.3 验证

```text
$env:PYTHONPATH="src"; python -m unittest tests.test_services -v
Ran 5 tests in 2.984s
OK

$env:PYTHONPATH="src"; python -m unittest discover -s tests
Ran 55 tests in 12.253s
OK          （原 50 条 + 重构回归 5 条）
```

| 检查 | 命令 / 方式 | 结果 |
|---|---|---|
| 文本输出逐字节一致 | `test_scan_text_output_matches_golden`（对比改动前快照） | 通过 |
| JSON 输出逐字节一致 | `test_scan_json_output_matches_golden` | 通过 |
| CLI 不再持有读取逻辑 | `Select-String cli.py -Pattern 'read_sources\|detect_schema\|extract_periods\|discover_files'` | **0 处匹配** |
| 退出码语义不变 | `scan --date 2026-11-07` → 0；`scan --date 1999-01-01` → 2 | 符合 |
| 空目录引导 | `test_preview_empty_source_reports_zero_and_cli_exits_2` | 通过（stderr 文案一致） |
| 目录不存在 | `test_preview_missing_source_raises` | 通过 |

> 黄金样本依赖 `examples/raw/` 的演示数据（已入库、阶段 1/2 不再改动）。若将来必须修改该目录，需同步重生成两份夹具并在此记录原因。

### 1.4 为什么这一步是阶段 1 的前置

`FR-2.6`「只预览不生成」要求界面先给出文件数、行数、日期列、告警，再让操作员决定是否运行。重构前这段逻辑锁在 CLI 私有函数里且与 `print` 耦合，GUI 只有两条路：复制一份读取逻辑（违反不变量 1），或调 `print` 出来的字符串（不可用）。现在 `services.preview()` 返回结构化数据，GUI 只是另一个消费者。

### 1.5 未做（刻意留给后续步骤）

- 未改 `detect_schema` 的 `header_scan` 参数（仍用默认 20，与重构前一致）——若要改成 `settings.header_scan_rows` 属于行为变更，必须单独立项并重生成黄金样本；
- 未接 GUI 任何界面；`gui/services.py`（GUI 侧薄封装）随阶段 1 界面功能一起建。

---

## 下一步（阶段 1 界面功能，按任务书顺序）

| 步骤 | 内容 | 对应 FR |
|---|---|---|
| 1 | 数据导入区：打开数据源目录、文件清单（名称/类型/大小/时间）、刷新、不支持类型标灰、空目录禁止空跑 | FR-1.1 / 1.2 / 1.3 / 1.6 |
| 2 | 运行控制：目标日期选择、开始分析（后台线程 + `execute()`）、四阶段进度、运行中禁用与锁检测 | FR-2.1 – 2.4 |
| 3 | 结果获取：打开 `cleaned_data.csv`、打开报告/所在文件夹、无结果时禁用并说明 | FR-3.1 – 3.3 |
| 4 | 质量与历史（阶段 2 起）：质量分、五类告警、历史记录 | FR-5.x / FR-6.1 |
