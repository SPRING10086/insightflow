"""LLM execution provider: generate code, then run it in the sandbox.

The model only produces code; numbers come from ``run_sandbox`` executing that
code on the local cleaned frame.  Any failure raises and the pipeline falls back
to the rule baseline.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..execution import CodeArtifact, ExecutionResult, ExecutionSpec
from ..semantic import FieldSemantics
from .client import CostLimitExceeded, DeepSeekClient, LLMConfig
from .contracts import ContractError, check_no_business_numbers
from .payloads import mask_value
from .sandbox import SandboxError, run_sandbox, validate_code
from .stats import LLMStats

EXECUTION_PROMPT_VERSION = "execution_v1"
PROMPT_FILE = Path(__file__).with_name("prompts") / "execution_v1.md"


class LLMExecutionProvider:
    name = "deepseek"

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Any | None = None,
        batch_budget_cny: float = 20.0,
        timeout_seconds: float = 10.0,
        memory_mb: int = 1024,
        prompt_text: str | None = None,
        stats: Any | None = None,
        warn_prompt_tokens: int = 8000,
    ) -> None:
        self.config = config
        self.transport = transport
        self.batch_budget_cny = float(batch_budget_cny)
        self.timeout_seconds = float(timeout_seconds)
        self.memory_mb = int(memory_mb)
        self.prompt_text = prompt_text or PROMPT_FILE.read_text(encoding="utf-8")
        self.spent_cny = 0.0
        self.last_code: CodeArtifact | None = None
        self._feedback = ""
        self.stats = stats or LLMStats()
        self.warn_prompt_tokens = int(warn_prompt_tokens)

    def _generate(self, spec: ExecutionSpec, frame, fields: list[FieldSemantics]) -> CodeArtifact:
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(f"整批预算已用尽（{self.spent_cny:.2f} 元），执行降级")

        samples = [
            {column: mask_value(row.get(column), column) for column in frame.columns if not str(column).startswith("__")}
            for row in frame.head(10).to_dict("records")
        ]
        payload = {
            "spec": spec.__dict__,
            "fields": [{"name": field.name, "role": field.role, "unit": field.unit} for field in fields],
            "samples": samples,
        }
        messages = [
            {"role": "system", "content": self.prompt_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        if self._feedback:
            messages.append({"role": "user", "content": self._feedback})
            self._feedback = ""
        client = DeepSeekClient(self.config, transport=self.transport)
        started = time.perf_counter()
        self.stats.record_call()
        content, usage = client.complete(messages)
        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.stats.add_usage(
            prompt_tokens,
            client.meter.cost_for(prompt_tokens, int(usage.get("completion_tokens") or 0)),
            self.warn_prompt_tokens,
        )
        try:
            data = json.loads(content, strict=False)
            if not isinstance(data, dict) or not isinstance(data.get("code"), str) or not data["code"].strip():
                raise ContractError("输出必须包含非空 code 字符串")
            check_no_business_numbers({key: value for key, value in data.items() if key != "code"})
        except json.JSONDecodeError as exc:
            self.stats.record_json_failure()
            raise ContractError(f"代码不是合法 JSON：{exc}") from exc
        except ContractError:
            self.stats.record_json_failure()
            raise
        code = data["code"]
        cost = client.meter.total_cny
        self.spent_cny += cost
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(f"整批预算已用尽（{self.spent_cny:.2f} 元）")
        return CodeArtifact(code=code, prompt_version=EXECUTION_PROMPT_VERSION, cost_cny=cost, latency_ms=latency_ms)

    def execute(
        self,
        spec: ExecutionSpec,
        frame,
        allowed_columns: list[str],
        fields: list[FieldSemantics] | None = None,
    ) -> ExecutionResult:
        last_error: Exception | None = None
        for attempt in range(2):
            code_artifact = self._generate(spec, frame, list(fields or []))
            self.last_code = code_artifact
            try:
                validate_code(code_artifact.code, allowed_columns)
                data = run_sandbox(
                    code_artifact.code,
                    frame,
                    allowed_columns=allowed_columns,
                    timeout_seconds=self.timeout_seconds,
                    memory_mb=self.memory_mb,
                )
            except (SandboxError, ContractError) as exc:
                last_error = exc
                if attempt == 1:
                    raise
                if self.stats.cost_cny >= self.batch_budget_cny:
                    self.stats.record_retry_blocked()
                    raise CostLimitExceeded("retry_blocked: 执行重试将超出整批预算") from None
                self.stats.record_retry()
                self._feedback = f"上次代码未通过校验：{exc}。不要造新列（assign），直接对 metric 列聚合。"
                continue
            return ExecutionResult(
                metrics={str(key): float(value) for key, value in data["metrics"].items()},
                groups=data["groups"],
                source_kind="llm",
                memory_guard="job",
                cost_cny=code_artifact.cost_cny,
                latency_ms=code_artifact.latency_ms,
            )
        raise last_error if last_error else RuntimeError("execution failed")
