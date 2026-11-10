# LLM 阶段 5 发布前置清单（代码签名 + 干净虚拟机）

> 性质：正式对外分发前的**硬门禁**。两项都需要外部资源（代码签名证书、干净 Windows 机器），
> 开发侧先交付可执行步骤与自动核验脚本；资源到位后由用户/运维执行并记录结果。
> 配套脚本：`scripts/verify_release.ps1`（自动核验，见第 4 节）。

## 1. 需要签名的文件

在 `dist/InsightFlow/` 下：

| 文件 | 说明 |
|---|---|
| `启动操作端.vbs` | 无黑框启动器（用户双击的入口），**必须签名** |
| `python/python.exe` | 包内解释器，**必须签名** |
| `python/pythonw.exe` | 包内无控制台解释器，**必须签名** |
| `python/python310.dll`、`python/vcruntime140.dll`、`python/*.dll` | 运行时依赖（建议一并签名，按实际存在文件） |
| `启动操作端(排障).bat` | 保留控制台的排障入口，**无法签名，见下方说明** |

> **平台限制（重要）**：Windows 没有为 `.bat`/`.cmd` 批处理注册 Authenticode SIP，
> 对批处理执行 `Set-AuthenticodeSignature` 会返回 *"The form specified for the subject is not
> supported"*，签名块无法写入；`Get-AuthenticodeSignature` 对任何 bat（即使从未签名）也返回
> UnknownError。这是 Windows 平台行为，不是证书或操作问题。该 bat 仅为技术排障备用入口，
> 普通用户走已签名的 `.vbs` 主入口，故不影响分发安全。`verify_release.ps1` 已将其排除在签名检查外。
>
> 对**可签名的可执行文件与启动器**全部签名；纯数据/文档文件不需要。签名后重新计算清单哈希（见 §4）。

## 2. 签名步骤（Windows SDK / signtool）

```powershell
# 1) 准备证书（pfx 或 USB token），确认时间戳服务可用
# 2) 对每个文件签名（示例域名按你的 CA 调整）
# 排障 .bat 无法签名（平台限制，见 §1），不在此清单内
$files = @(
  "dist\InsightFlow\启动操作端.vbs",
  "dist\InsightFlow\python\python.exe",
  "dist\InsightFlow\python\pythonw.exe"
)
foreach ($f in $files) {
  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a $f
}

# 3) 验证签名
foreach ($f in $files) {
  Get-AuthenticodeSignature -FilePath $f | Select-Object Status, SignerCertificate
}
```

验收：每个文件 `Status = Valid`；双击启动器 Windows 不弹"未知发布者"（SmartScreen 仍可能提示，属声誉问题）。

## 3. 干净虚拟机验证（Windows 10/11，未装 Python）

1. **准备**：全新虚拟机，无 Python、无本项目依赖；安装 **VC++ 2015–2022 x64 Redistributable**
   （`vc_redist.x64.exe`），重启。
2. **拷贝**：把签名后的 `dist/InsightFlow` 整个目录拷入 VM（不要用本机路径共享运行）。
3. **清单核验**：在 VM 上运行
   `powershell -ExecutionPolicy Bypass -File scripts\verify_release.ps1 -Dist <路径> -RequireSignature`
   要求全部 PASS（含签名、哈希、冒烟证据）。
4. **启动**：双击 `启动操作端.vbs` → 窗口打开、**无命令行黑框**、`logs/insightflow.log` 出现启动行。
5. **默认关闭**：直接运行一次 → 生成 HTML；确认没有 `semantic_* / plan_* / execution_* / narrative_*` 产物；
   用 `Get-NetTCPConnection`/防火墙日志确认无出站连接。
6. **开关冒烟**：在 GUI 打开任一 LLM 开关但**不配置密钥** → 运行应成功并提示"已降级"；报告中无
   "智能解读" 区块（叙事关闭时）。
7. **有密钥场景**（可选，在隔离网络）：放 `.env`（不进分发包）→ 开启 semantic/plan/execute/narrative
   → 确认只访问配置的 API 域名、成本显示、对账结论；验证后删除 `.env`。
8. **卸载检查**：删除目录后无残留（无注册表写入、无服务）。

## 4. 自动核验脚本

```powershell
# 生成/更新哈希清单（首次或签名/重打包后）
powershell -ExecutionPolicy Bypass -File scripts\verify_release.ps1 -WriteManifest

# 正式核验（未签名开发包可加 -SkipSignature 只看其他项）
powershell -ExecutionPolicy Bypass -File scripts\verify_release.ps1 -SkipSignature

# 对外发布前（要求签名有效）
powershell -ExecutionPolicy Bypass -File scripts\verify_release.ps1 -RequireSignature
```

脚本检查项：

| 检查 | 说明 |
|---|---|
| 必需文件 | 解释器、启动器、`app/src`、`requirements.lock.txt` |
| 无秘密 | 不含 `.env`（允许 `.env.example`）、文本中无 `sk-` 长串 |
| 版本一致 | `app/src/insightflow/__init__.py` 与 `app/config/config.json` 的版本一致 |
| 哈希清单 | `dist/InsightFlow.sha256` 存在且逐文件匹配（`-WriteManifest` 生成） |
| 签名 | `Get-AuthenticodeSignature` 逐个校验；`-RequireSignature` 时未签名即 FAIL |
| 冒烟证据 | `docs/llm-stage5-smoke.json` 为 17/17 PASS |
| VC++ 运行库 | `vcruntime140.dll` / `msvcp140.dll` 存在（否则 WARN，VM 需安装） |

退出码：有 FAIL → 1；全 PASS（含 WARN）→ 0。

## 5. 记录模板

| 项 | 结果 | 执行人 | 日期 | 证据 |
|---|---|---|---|---|
| 代码签名 | 待执行 | | | `Get-AuthenticodeSignature` 输出 |
| 干净 VM 启动 | 待执行 | | | 截图 + `logs/insightflow.log` |
| 默认关闭无出站 | 待执行 | | | 连接列表/防火墙日志 |
| 开关降级 | 待执行 | | | GUI 截图 + `run_record.json` |
| `verify_release.ps1` | 待执行 | | | 脚本输出（PASS/FAIL 表） |

> 未完成以上任一项，不得对外分发。开发侧已完成的自动化项（冒烟、无秘密、哈希、版本）见
> `docs/llm-stage5-smoke.json` 与 `docs/llm-stage5-notes.md`。
