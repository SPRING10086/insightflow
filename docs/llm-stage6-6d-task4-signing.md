# 6d 任务 4 阶段 A：本机重签与产物就绪记录

- 执行时间：2026-11-11
- 源码基线：`2db8d6b`（375 tests、灰度 ready=true、覆盖率 87.6%）
- dist：`dist/InsightFlow`（198 MB，冻结后未再改源码）
- 签名工具：`scripts/sign_dist.ps1`

## 1. 签名证书

| 项 | 值 |
|---|---|
| Subject | `CN=InsightFlow Test Code Signing` |
| Thumbprint | `DC260210D6919B88DEF0BD53C81732F286C6C978` |
| 有效期 | 至 2027-11-10 |
| 用途 | **仅内部/测试**；自签名只在安装对应根证书的机器上有效，**不得冒充公网正式证书** |
| 公钥文件 | `dist/InsightFlow/InsightFlow-TestRoot.cer`（随包） |

## 2. 签名结果

| 文件 | 签名前 | 签名后 |
|---|---|---|
| `启动操作端.vbs` | NotSigned | **Valid** |
| `python/python.exe` | Valid | **Valid** |
| `python/pythonw.exe` | Valid | **Valid** |
| `python/python3.dll` | Valid | **Valid** |
| `python/python310.dll` | Valid | **Valid** |
| `python/vcruntime140.dll` | Valid | **Valid** |
| `python/vcruntime140_1.dll` | Valid | **Valid** |

`.bat` 因平台 SIP 限制不签名（release-checklist 已记录）。

## 3. 哈希清单与验签

- 签名会改变文件字节，已重新执行 `verify_release.ps1 -WriteManifest`：
  `InsightFlow.sha256` 共 **9 个文件**，逐文件匹配。
- 本机已把公钥装入 `Cert:\CurrentUser\Root` 后执行：

```text
powershell -ExecutionPolicy Bypass -File scripts\verify_release.ps1 -RequireSignature
结果：FAIL=0 WARN=0 / 共 8 项
required_files PASS · no_dotenv PASS · no_api_key PASS · version PASS
manifest PASS (9 files match) · signatures PASS (all valid)
smoke_evidence PASS (17/17) · vcpp_runtime PASS
```

- bundled 解释器冒烟：**17 passed, 0 failed**。

## 4. 复现命令

```powershell
cd insightflow
powershell -ExecutionPolicy Bypass -File scripts\sign_dist.ps1
powershell -ExecutionPolicy Bypass -File scripts\verify_release.ps1 -RequireSignature
```

> 任何重新打包都会使签名失效，必须重新执行上述两条命令并重跑冒烟。

## 5. 未完成（依赖外部资源）

- 干净 Win10/11 VM 回归：按 `docs/llm-stage6-6d-task4-vm-signoff.md` 执行并回填签核表；
- 公网正式分发如需 CA 证书，由用户提供后按同一脚本流程重签（自签名不可用于公网）。
