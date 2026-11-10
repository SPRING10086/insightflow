# 阶段 6 进度记录与交接（暂停点）

- 记录时间：2026-11-10 18:55（Asia/Shanghai）
- 当前分支：`feat/llm-poc`
- 当前 HEAD：`801875f feat(llm): stage-6b-2 execute adoption with mixed-source labelling`
- 状态：**6b-2 已提交；6b-2 的覆盖率门禁复跑被用户中止，尚未拿到最终数字**（见 §4）。

## 1. 已完成（阶段 0–5，均已复核通过）

| 阶段 | 提交 | 内容 |
|---|---|---|
| 0 | `8dc7e1c`/`54bcd30` | LLM PoC：脱敏载荷、契约、成本护栏 |
| 1 | `baf6305`/`480d8ab` | 语义 Schema 双轨（20/20、prompt v3） |
| 2 | `b162cee`…`75f931f` | 分析计划双轨、原始全列、闭环校验 |
| 3 | `6507e82`/`96eb022` | 受控执行：AST/沙箱/Job/对账 |
| 4 | `1eaf9f2`/`59c143d` | 占位符叙事、XSS 防护 |
| 5a | `e73db14`/`3bc2dcc`/`9e1f534`/`88fa5ca` | 统一配置、便携冒烟、JSON 稳定性、发布核验 |
| 5b | `f242852` | GUI 四开关/成本/对账/文档 |
| 4 收尾 | `9823121` | 签名核验 `-FilePath` 修复、bat 平台限制 |

## 2. 阶段 6 已完成

| 子阶段 | 提交 | 内容 |
|---|---|---|
| 6a | `078fbdc` | 采信模式框架：`force_rules` + 四能力 `adoption`（默认全 shadow）、依赖降级、kill switch；默认零行为变化 |
| 6b-1 | `c4b34dc` | 三项复核对齐（env 优先、`final_mode/downgrade_reason/force_rules`、narrative 依赖 execute）+ narrative 采信（shadow 不渲染、gated/primary 校验后渲染、失败回退） |
| 6b-2 | `801875f` | execute 采信过渡方案 (b)：只采信已对账的主指标合计；报告顶部“数据来源”标注混合来源；`metadata.execution` 记录 `adopted/adopted_metrics/rule_metrics/gate_reason`；失败回退且 `status=success` |

## 3. 最近一次验证结果

```text
Ran 302 tests  OK                 （6b-2 提交后全量）
便携包冒烟 17/17 PASS             （dist 198 MB，重建于 6b-2 代码）
verify_release.ps1：FAIL=0 WARN=1  （签名跳过）
```

> 注意：为验证 6b-2 重建了 `dist`，**原有自签名已失效**；分发前必须重签并跑
> `verify_release.ps1 -RequireSignature`（见 `docs/release-checklist-llm.md`）。

## 4. 被中止的工作（恢复后第一件事）

**6b-2 覆盖率门禁复跑**：`python scripts/coverage_gate.py --threshold 85` 在后台运行约 18 分钟，
日志 `logs/gate_6b2.log` 末尾出现已知的 GUI 后台线程日志 `FileNotFoundError` 噪声，尚未打印覆盖率；
用户要求停止，任务已取消。

恢复后建议：

```powershell
cd insightflow
$env:PYTHONPATH='src'
python scripts\coverage_gate.py --threshold 85
# 参考基线：6b-1 为 87.0%（4758/5469），6a 为 87.0%
```

若再次出现 GUI 线程噪声导致 `errors=1`，先单独复跑 `tests.test_gui_shell test_gui_stage1..4` 确认；
该 flake 属环境敏感项（临时目录删除后后台线程写日志），非代码缺陷。

## 5. 未完成（后续顺序）

1. **6b-3 plan 采信**：核心侧纯函数 `PlanDraft → AnalysisPlan` 适配器（无网络/无 llm import、黄金样本、非法输入拒绝）；门槛通过后由适配器产出的计划驱动 `analyze()`；失败回退规则计划。
2. **6b-4 semantic 采信**：最严门槛——业务列全覆盖、`date/id/metric` 关键角色不得冲突、字段可映射、单位冲突回退；冲突整表回退。
3. **6c 双轨对照与灰度观测**：扩展 `llm_stats`（采信率/回退率/对账通过率/一致性）；规则结果继续作为对照；样本多样性 ≥3 类、N≥10 后才可进入 6d。
4. **6d 驱动切换**：gated→primary；**必须先把对账从 (b) 升级到 (a) 全指标对账**；GUI 模式文案、运维/发布文档、干净 VM 回归。
5. **发布前置**：重签 dist + `verify_release.ps1 -RequireSignature`；干净 VM 实际执行（清单已交付，资源未到位）。

## 6. 已知风险/未根治项

- `dist/` 当前未签名（重建所致）。
- 真实工商样本偶发 JSON 不闭合（重试+降级可兜底，但会计入回退率）。
- GUI 日志线程 flake、沙箱内存测试在高负载下偶发误报；已把非内存类沙箱测试上限提到 2GB，仍建议 CI 串行。
- 阶段 5 迁移提示：开启 narrative feature 不再自动渲染，必须显式 `adoption.narrative=gated/primary`（且 execute 非 shadow）。

## 7. 工作区未跟踪项（未提交，保持原样）

- `acceptance_data/`
- `docs/llm-stage6-design.md`、`docs/llm-stage6-review.md`
- `docs/任务书_签名核验修复入库.md`

如需入库/清理，另行确认。
