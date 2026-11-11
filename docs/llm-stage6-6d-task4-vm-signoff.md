# 6d 任务 4 阶段 B/C：干净 VM 回归操作手册与签核模板

> 责任方：用户/运维（外部 VM）。开发侧已冻结并签名 dist，本文件可直接照做。
> 依据：`docs/release-checklist-llm.md` 第 3/5 节。

## 0. 准备物

- 干净的 Windows 10/11（**未安装 Python、未装本项目依赖**），建议全新快照；
- VC++ 2015–2022 x64 Redistributable（安装后重启）；
- 已签名 `dist/InsightFlow` 整个目录；
- `InsightFlow-TestRoot.cer`（在 dist 根目录）；
- `scripts/verify_release.ps1`（从源码目录拷到 VM 任意位置，例如 `C:\verify\verify_release.ps1`）；
- 本文件用于回填证据。

## 1. 安装信任根（自签名验证必需）

以管理员 PowerShell：

```powershell
Import-Certificate -FilePath .\InsightFlow-TestRoot.cer -CertStoreLocation Cert:\LocalMachine\Root
Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -like '*InsightFlow*'
```

> 不装根证书时签名状态会是 UntrustedRoot，`-RequireSignature` 会 FAIL；这是自签名预期行为。

## 2. 验签 + 清单（硬指标）

```powershell
powershell -ExecutionPolicy Bypass -File C:\verify\verify_release.ps1 `
  -Dist C:\InsightFlow -RequireSignature
```

要求：**FAIL=0 WARN=0**，`manifest` 逐文件匹配、`signatures` 全部 Valid、`smoke_evidence` PASS。
把完整输出表截图/复制留证。

## 3. 功能与安全回归

| # | 操作 | 期望 | 证据 |
|---|---|---|---|
| 1 | 双击 `启动操作端.vbs` | 窗口打开、**无命令行黑框**、`logs/insightflow.log` 有启动行 | 截图 + 日志片段 |
| 2 | 默认关闭（LLM 未开）跑一次分析 | 生成 HTML；无 `semantic_*/plan_*/execution_*/narrative_*` 产物 | 目录截图 + `Get-ChildItem` 输出 |
| 3 | 同次运行抓网络 | 无出站连接（`Get-NetTCPConnection` 或防火墙日志） | 命令输出 |
| 4 | GUI 打开任一 LLM 开关但不配密钥，运行 | 成功并提示“已降级”；无“智能解读”区块；无崩溃 | 截图 + 日志 |
| 5 | （可选）配 `.env` 开四能力、隔离网络 | 只访问配置的 API 域名；成本与对账显示正确；验后删除 `.env` | 截图 + 抓包/日志 |
| 6 | 删除安装目录 | 无注册表写入、无服务、无计划任务残留 | `reg query`/`Get-Service`/`Get-ScheduledTask` 输出 |

## 4. 签核表（回填）

| 项 | 结果 | 执行人 | 日期 | 证据位置 |
|---|---|---|---|---|
| 根证书安装 |  |  |  |  |
| verify `-RequireSignature` 零 FAIL |  |  |  |  |
| 双击 .vbs 无黑框启动 |  |  |  |  |
| 默认关闭无 LLM 产物 |  |  |  |  |
| 默认关闭无出站连接 |  |  |  |  |
| 无密钥开关安全降级 |  |  |  |  |
| 有密钥隔离访问（可选） |  |  |  |  |
| 卸载无残留 |  |  |  |  |

## 5. 回滚

- 验签失败：先确认根证书装在 `LocalMachine\Root`，再核对 `InsightFlow.sha256` 是否在签名后重算；
- 启动失败：不要重装，先保留 `logs/` 与错误截图，回到源码侧重新签名/打包（`scripts/sign_dist.ps1`）；
- 仍不通过则任务 4 不闭环，**禁止分发**。
