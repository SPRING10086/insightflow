# 阶段 4 交付记录：打包发布与管理员能力

> 依据：《操作端前端要求书》FR-7.2、NFR-7、NFR-8、NFR-6（GUI 测试入 CI）
> 阶段目标：消除控制台黑框、产出可分发产物、完成安装与启动验证。

---

## 1. 交付一览

| 项 | 实现 | 证据 |
|---|---|---|
| 消除控制台黑框 | 新增 `启动操作端(无控制台).vbs`：`WScript.Shell.Run ..., 0, False` 启动 `pythonw.exe`，全程无控制台窗口 | 实测进程名为 `pythonw.exe`，窗口标题 `InsightFlow 操作端`，关窗后进程归零 |
| 免安装分发包（NFR-7） | `scripts/build_portable.ps1` 生成 `dist/InsightFlow/`：随包 Python 3.10（含锁定 pandas/numpy）+ `app/` + 两个启动器 + 使用说明 | 构建 207 MB；干净环境导入校验 `imports ok 2.3.3`；从包内 VBS 启动成功 |
| 图形化装卸计划任务（FR-7.2） | 「管理员设置」新增：每日触发时间输入 + 「安装/更新任务」「卸载任务」，均二次确认；安装/卸载调用核心 `scheduler` | 单测：调用核心、坏时间拒绝、失败透传、取消不执行 |
| 可访问性（NFR-8） | 所有颜色提示都有文字：质量分旁标「优秀/良好/需关注/较差」、告警以「【类型】」开头、灰色文件标「不支持的类型」、环境徽标写文字而非仅颜色 | 单测 `test_state_is_readable_without_colour` |
| GUI 测试入 CI（NFR-6） | `tests.yml` 增加显式 GUI 测试步骤与离线检查步骤；新增 `portable.yml`（手动/打 tag 时构建并上传免安装包） | 工作流文件已提交 |
| 离线校验 | 单测扫描 `src/**/*.py`，禁止 `requests/socket/urllib/http/aiohttp/ftplib` 等网络导入 | `test_source_has_no_network_imports` |

## 2. 免安装包怎么用

```text
dist/InsightFlow/
├─ 启动操作端.vbs         ← 双击这个（无黑框）
├─ 启动操作端(排障).bat   ← 出问题时用这个看错误
├─ 使用说明.txt
├─ python/                ← 随包 Python 3.10.4 + pandas 2.3.3 / numpy 2.2.6（无需安装）
└─ app/                   ← 程序、配置、示例数据（config/config.json 为实际配置）
```

构建：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1
```

脚本会：校验解释器是 3.10 且依赖匹配锁定版本 → 复制解释器（排除测试/文档等）→ 复制应用（排除 logs/state/output/.git）→ 生成启动器与说明 → 用包内解释器做导入冒烟 → 打印体积。

## 3. 验证

```text
$env:PYTHONPATH="src"; python -m unittest tests.test_gui_stage4 -v
Ran 9 tests in 5.384s
OK

$env:PYTHONPATH="src"; python -m unittest discover -s tests
Ran 96 tests in 65.295s
OK          （87 + 9）
```

| 检查 | 结果 |
|---|---|
| 分发包启动 | VBS → `pythonw.exe` → 窗口 `InsightFlow 操作端`；关窗后无残留进程 |
| 无控制台 | 进程为 `pythonw.exe`（无 console），VBS 使用 window style 0 |
| 干净环境可用 | 包内解释器在清空 `PYTHONPATH/PYTHONHOME` 后仍能导入应用与 pandas |
| 调度安装/卸载 | 全部经核心 `scheduler.install_windows_task / remove_windows_task`，UI 仅编排 |
| 核心零改动 | 本阶段只改 `gui/`、测试、脚本、CI、文档（`pipeline/analysis/report/...` 未触碰） |

## 4. 已知限制（如实登记）

1. **未做单文件 .exe**：PyInstaller 不在锁定依赖里，且打包需引入构建期依赖。按 NFR-7"解压即用"口径交付目录式分发包；若必须单文件 exe，需评审批准引入 PyInstaller 并新增构建流水线。
2. **依赖系统 VC++ 运行库**：Python 3.10 需要 Microsoft VC++ 2015-2022 Redistributable（Win10/11 通常自带）。干净机器若提示缺少 DLL，安装该运行库即可，`使用说明.txt` 未包含安装包。
3. **未签名**：首次运行可能被 SmartScreen 提示"未知发布者"，需操作员点"仍要运行"；正式分发应做代码签名。
4. **首次启动稍慢**：窗口化启动需加载 pandas/numpy（约 1-3 秒），属正常。
5. **分发包体积 207 MB**：随包解释器与科学计算库所致；如需压缩可再打 zip（构建脚本未做压缩）。

## 5. 口径提醒（与复核结论一致）

本阶段只完成 **GUI 操作端**的打包与管理员能力。核心商业级门槛的 **P2 三项仍未闭环**：

1. 中位数填补双轨口径（有效值 / 含填补）；
2. 混合单位（表头单位 + 单元格单位）歧义；
3. CI 覆盖率门槛 + 独立对账脚本。

在三项闭环前，系统仍为**有条件放行**：可日常使用、保留人工复核，不宣称无条件商业级。
