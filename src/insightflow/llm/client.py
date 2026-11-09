"""LLM PoC: minimal stdlib-only DeepSeek chat client with cost guard rails.

Design notes:
* no SDK dependency (``urllib`` + ``json`` only);
* the model identifier, price and key all come from configuration — the code
  never hardcodes a model string, so an unverified id cannot leak into code;
* 429/5xx/network failures retry with backoff, everything else fails loudly;
* every call is metered; crossing the warn/stop thresholds is an error, not a log line.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_BASE_URL = "https://api.deepseek.com"
CHAT_PATH = "/chat/completions"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMConfigError(RuntimeError):
    """Missing or invalid configuration (key/model/price)."""


class LLMRequestError(RuntimeError):
    """HTTP/transport failure that survived retries."""


class CostLimitExceeded(RuntimeError):
    """The per-table cost guard tripped."""


def load_dotenv(path: str | os.PathLike[str]) -> dict[str, str]:
    """Minimal ``.env`` reader (KEY=VALUE, ``#`` comments, optional quotes)."""
    file = Path(path)
    if not file.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        text = file.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


@dataclass
class LLMConfig:
    model: str
    api_key: str
    price_in_cny_per_mtok: float
    price_out_cny_per_mtok: float
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 60.0
    max_retries: int = 2
    max_output_tokens: int = 2000
    temperature: float = 0.0
    cost_warn_cny: float = 4.5
    cost_stop_cny: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMConfig":
        if env is None:
            merged: dict[str, str] = {}
            for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"):
                merged.update(load_dotenv(candidate))
            # A real environment variable always wins over a local .env file.
            merged.update(os.environ)
            source: Mapping[str, str] = merged
        else:
            source = env

        def get(name: str, default: str = "") -> str:
            return str(source.get(name, default) or "").strip()

        model = get("INSIGHTFLOW_LLM_MODEL")
        api_key = get("DEEPSEEK_API_KEY") or get("INSIGHTFLOW_LLM_API_KEY")
        price_in = get("INSIGHTFLOW_LLM_PRICE_IN")
        price_out = get("INSIGHTFLOW_LLM_PRICE_OUT")

        missing = [
            name
            for name, value in (
                ("INSIGHTFLOW_LLM_MODEL", model),
                ("DEEPSEEK_API_KEY", api_key),
                ("INSIGHTFLOW_LLM_PRICE_IN", price_in),
                ("INSIGHTFLOW_LLM_PRICE_OUT", price_out),
            )
            if not value
        ]
        if missing:
            raise LLMConfigError(
                "缺少配置：" + "、".join(missing) + "。\n"
                "请在环境变量或本地 .env 中填写；模型标识必须使用实测可用的 API 名称，"
                "单价按官方定价（人民币/百万 tokens）填写，代码不内置默认单价。"
            )
        try:
            in_price = float(price_in)
            out_price = float(price_out)
        except ValueError as exc:
            raise LLMConfigError("单价必须是数字（人民币/百万 tokens）") from exc

        return cls(
            model=model,
            api_key=api_key,
            price_in_cny_per_mtok=in_price,
            price_out_cny_per_mtok=out_price,
            base_url=get("INSIGHTFLOW_LLM_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
            timeout_seconds=float(get("INSIGHTFLOW_LLM_TIMEOUT", "60") or 60),
            max_retries=int(get("INSIGHTFLOW_LLM_RETRIES", "2") or 2),
            max_output_tokens=int(get("INSIGHTFLOW_LLM_MAX_TOKENS", "2000") or 2000),
        )


@dataclass
class CostMeter:
    """Track spend per table and enforce the 4.5/5.0 CNY guard rails."""

    price_in_cny_per_mtok: float
    price_out_cny_per_mtok: float
    warn_cny: float = 4.5
    stop_cny: float = 5.0
    total_cny: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    warned: bool = False
    calls: int = 0

    def cost_for(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1_000_000 * self.price_in_cny_per_mtok
            + completion_tokens / 1_000_000 * self.price_out_cny_per_mtok
        )

    def add(self, prompt_tokens: int, completion_tokens: int) -> float:
        self.calls += 1
        self.prompt_tokens += int(prompt_tokens)
        self.completion_tokens += int(completion_tokens)
        self.total_cny += self.cost_for(int(prompt_tokens), int(completion_tokens))
        if self.total_cny >= self.warn_cny:
            self.warned = True
        if self.total_cny >= self.stop_cny:
            raise CostLimitExceeded(
                f"单表成本 {self.total_cny:.2f} 元，达到硬停阈值 {self.stop_cny:.2f} 元"
            )
        return self.total_cny

    def guard(self) -> None:
        if self.total_cny >= self.stop_cny:
            raise CostLimitExceeded(
                f"单表成本已达 {self.total_cny:.2f} 元（阈值 {self.stop_cny:.2f}），中止调用"
            )

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_cny": round(self.total_cny, 4),
            "warned": self.warned,
        }


#: transport(url, headers, payload, timeout) -> (status_code, body_text)
Transport = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, str]]


def urllib_transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"transport-error: {exc}"


class DeepSeekClient:
    def __init__(self, config: LLMConfig, transport: Transport | None = None, sleep: Callable[[float], None] | None = None):
        self.config = config
        self.transport = transport or urllib_transport
        self.sleep = sleep or time.sleep
        self.meter = CostMeter(
            price_in_cny_per_mtok=config.price_in_cny_per_mtok,
            price_out_cny_per_mtok=config.price_out_cny_per_mtok,
            warn_cny=config.cost_warn_cny,
            stop_cny=config.cost_stop_cny,
        )

    def _endpoint(self) -> str:
        return self.config.base_url.rstrip("/")

    def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int]]:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        last_error = "未知错误"
        for attempt in range(self.config.max_retries + 1):
            self.meter.guard()
            status, text = self.transport(self._endpoint(), headers, payload, self.config.timeout_seconds)
            if status == 200:
                content, usage = _parse_chat_response(text)
                self.meter.add(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                return content, usage
            last_error = f"HTTP {status}: {text[:300]}"
            if status in RETRYABLE_STATUS or status == 0:
                if attempt < self.config.max_retries:
                    self.sleep(0.5 * (2**attempt))
                    continue
            raise LLMRequestError(last_error)
        raise LLMRequestError(last_error)


def _parse_chat_response(text: str) -> tuple[str, dict[str, int]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMRequestError(f"响应不是合法 JSON：{exc}") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(f"响应缺少 choices[0].message.content：{text[:300]}") from exc
    usage_raw = data.get("usage") or {}
    usage = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
        "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
        "total_tokens": int(usage_raw.get("total_tokens") or 0),
    }
    return str(content), usage
