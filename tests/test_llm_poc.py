from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm import contracts, evaluate, payloads  # noqa: E402
from insightflow.llm.client import (  # noqa: E402
    CostLimitExceeded,
    CostMeter,
    DeepSeekClient,
    LLMConfig,
    LLMConfigError,
    LLMRequestError,
)

VALID_ANSWER = {
    "semantic_schema": {
        "fields": [
            {"name": "订单日期", "role": "date", "unit": "", "meaning": "下单日期", "confidence": 0.95},
            {"name": "区域", "role": "dimension", "unit": "", "meaning": "销售大区", "confidence": 0.9},
            {"name": "销售额", "role": "metric", "unit": "元", "meaning": "成交金额", "confidence": 0.95},
        ]
    },
    "analysis_plan": {
        "questions": ["哪个区域贡献最大？"],
        "metrics": ["销售额"],
        "dimensions": ["区域"],
        "charts": [
            {
                "chart_id": "chart_1",
                "chart_type": "bar",
                "title": "区域销售额",
                "x_field": "区域",
                "y_field": "销售额",
                "option_skeleton": {
                    "xAxis": {"type": "category", "data": []},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": []}],
                },
            }
        ],
        "quality_focus": ["日期口径"],
    },
}


def _chat_response(content: dict | str) -> str:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return json.dumps(
        {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        },
        ensure_ascii=False,
    )


def _config(**overrides) -> LLMConfig:
    base = dict(
        model="test-model",
        api_key="test-key",
        price_in_cny_per_mtok=1.0,
        price_out_cny_per_mtok=1.0,
        max_retries=2,
    )
    base.update(overrides)
    return LLMConfig(**base)


class PayloadTests(unittest.TestCase):
    def _rows(self, count: int = 25) -> list[dict[str, str]]:
        rows = []
        for index in range(count):
            rows.append(
                {
                    "订单日期": "2026-11-07",
                    "客户名称": f"张三{index}",
                    "联系电话": "13812345678",
                    "邮箱": "zhangsan@example.com",
                    "公司": "华为技术有限公司",
                    "地址": "深圳市南山区科技园1号",
                    "销售额": str(100 + index),
                }
            )
        if len(rows) > 11:
            rows[11]["销售额"] = "SENTINEL-ROW-11"
        return rows
    def test_only_first_ten_rows_leave_and_row_eleven_never_appears(self):
        rows = self._rows()
        names = list(rows[0])
        payload = payloads.build_payload("t", "src.csv", names, rows)
        self.assertEqual(len(payload["samples"]), 10)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("SENTINEL-ROW-11", serialized)
        self.assertEqual(payload["table"]["row_count_exposed"], 10)
        self.assertTrue(payload["policy"]["full_data_leaves_local"])

    def test_sensitive_values_are_masked(self):
        rows = self._rows(3)
        names = list(rows[0])
        payload = payloads.build_payload("t", "src.csv", names, rows)
        text = json.dumps(payload["samples"], ensure_ascii=False)
        for raw in ("13812345678", "zhangsan@example.com", "华为技术有限公司", "深圳市南山区科技园1号", "张三0"):
            self.assertNotIn(raw, text)
        self.assertIn("138****5678", text)
        self.assertIn("zh***@example.com", text)
        self.assertIn("***", text)

    def test_dtype_inference(self):
        rows = [
            {"日期": "2026-11-07", "金额": "1,200.50", "名称": "华东"},
            {"日期": "2026-11-08", "金额": "300", "名称": "华南"},
        ]
        payload = payloads.build_payload("t", "s", list(rows[0]), rows)
        dtypes = {field["name"]: field["dtype"] for field in payload["fields"]}
        self.assertEqual(dtypes["金额"], "number")
        self.assertEqual(dtypes["日期"], "date")
        self.assertEqual(dtypes["名称"], "text")

    def test_assert_minimal_rejects_more_rows(self):
        payload = {"fields": [{"name": "a"}], "samples": [{"a": "1"}] * 11}
        with self.assertRaises(AssertionError):
            payloads.assert_payload_minimal(payload, 10)


class ContractTests(unittest.TestCase):
    def test_valid_answer_parses(self):
        result = contracts.parse_result(json.dumps(VALID_ANSWER, ensure_ascii=False), {"prompt_tokens": 1})
        self.assertEqual(len(result.fields), 3)
        self.assertEqual(result.fields[2].role, "metric")
        self.assertEqual(result.plan.charts[0].chart_type, "bar")
        self.assertEqual(result.usage["prompt_tokens"], 1)

    def test_missing_sections_rejected(self):
        with self.assertRaises(contracts.ContractError):
            contracts.parse_result("{}")
        with self.assertRaises(contracts.ContractError):
            contracts.parse_result("not json")

    def test_bad_role_rejected(self):
        broken = json.loads(json.dumps(VALID_ANSWER, ensure_ascii=False))
        broken["semantic_schema"]["fields"][0]["role"] = "currency"
        with self.assertRaises(contracts.ContractError):
            contracts.parse_result(json.dumps(broken, ensure_ascii=False))

    def test_confidence_bounds_enforced(self):
        broken = json.loads(json.dumps(VALID_ANSWER, ensure_ascii=False))
        broken["semantic_schema"]["fields"][0]["confidence"] = 1.5
        with self.assertRaises(contracts.ContractError):
            contracts.parse_result(json.dumps(broken, ensure_ascii=False))

    def test_non_empty_data_slots_rejected(self):
        broken = json.loads(json.dumps(VALID_ANSWER, ensure_ascii=False))
        broken["analysis_plan"]["charts"][0]["option_skeleton"]["series"][0]["data"] = [1, 2, 3]
        with self.assertRaises(contracts.ContractError):
            contracts.parse_result(json.dumps(broken, ensure_ascii=False))

    def test_business_numbers_rejected(self):
        broken = json.loads(json.dumps(VALID_ANSWER, ensure_ascii=False))
        broken["analysis_plan"]["total"] = 12345
        with self.assertRaises(contracts.ContractError):
            contracts.parse_result(json.dumps(broken, ensure_ascii=False))


class ClientTests(unittest.TestCase):
    def _messages(self) -> list[dict[str, str]]:
        return [{"role": "user", "content": "hi"}]

    def test_retry_on_429_then_success(self):
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            if len(calls) == 1:
                return 429, "rate limited"
            return 200, _chat_response(VALID_ANSWER)

        client = DeepSeekClient(_config(), transport=transport, sleep=lambda _s: None)
        content, usage = client.complete(self._messages())
        self.assertEqual(len(calls), 2)
        self.assertEqual(usage["total_tokens"], 1500)
        self.assertIn("semantic_schema", content)

    def test_401_fails_without_retry(self):
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 401, "unauthorized"

        client = DeepSeekClient(_config(), transport=transport, sleep=lambda _s: None)
        with self.assertRaises(LLMRequestError):
            client.complete(self._messages())
        self.assertEqual(len(calls), 1)

    def test_transport_error_retries_then_raises(self):
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 0, "transport-error"

        client = DeepSeekClient(_config(max_retries=1), transport=transport, sleep=lambda _s: None)
        with self.assertRaises(LLMRequestError):
            client.complete(self._messages())
        self.assertEqual(len(calls), 2)

    def test_cost_meter_trips_hard_stop(self):
        def transport(url, headers, payload, timeout):
            return 200, _chat_response(VALID_ANSWER)

        expensive = _config(price_in_cny_per_mtok=5000.0, price_out_cny_per_mtok=5000.0)
        client = DeepSeekClient(expensive, transport=transport, sleep=lambda _s: None)
        with self.assertRaises(CostLimitExceeded):
            client.complete(self._messages())
        self.assertGreaterEqual(client.meter.total_cny, 5.0)

    def test_guard_blocks_after_limit(self):
        meter = CostMeter(price_in_cny_per_mtok=1.0, price_out_cny_per_mtok=1.0)
        meter.total_cny = 5.5
        with self.assertRaises(CostLimitExceeded):
            meter.guard()

    def test_meter_records_tokens_and_cost(self):
        meter = CostMeter(price_in_cny_per_mtok=1000.0, price_out_cny_per_mtok=1000.0)
        meter.add(1000, 500)
        self.assertAlmostEqual(meter.total_cny, 1.5, places=6)
        self.assertTrue(meter.warned is False)

    def test_config_requires_model_key_and_prices(self):
        with self.assertRaises(LLMConfigError) as ctx:
            LLMConfig.from_env({})
        message = str(ctx.exception)
        for name in ("INSIGHTFLOW_LLM_MODEL", "DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_PRICE_IN"):
            self.assertIn(name, message)

    def test_config_reads_environment(self):
        config = LLMConfig.from_env(
            {
                "INSIGHTFLOW_LLM_MODEL": "measured-model-name",
                "DEEPSEEK_API_KEY": "k",
                "INSIGHTFLOW_LLM_PRICE_IN": "2",
                "INSIGHTFLOW_LLM_PRICE_OUT": "8",
            }
        )
        self.assertEqual(config.model, "measured-model-name")
        self.assertEqual(config.price_out_cny_per_mtok, 8.0)

    def test_dotenv_is_loaded_when_environment_is_absent(self):
        import os
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / ".env").write_text(
            "# comment\n"
            "INSIGHTFLOW_LLM_MODEL=dotenv-model\n"
            "DEEPSEEK_API_KEY='dotenv-key'\n"
            "INSIGHTFLOW_LLM_PRICE_IN=1.5\n"
            "INSIGHTFLOW_LLM_PRICE_OUT=6\n",
            encoding="utf-8",
        )
        keys = ("INSIGHTFLOW_LLM_MODEL", "DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_PRICE_IN", "INSIGHTFLOW_LLM_PRICE_OUT")
        saved = {key: os.environ.pop(key, None) for key in keys}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in saved.items() if v is not None])
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            from unittest.mock import patch
            with patch("insightflow.llm.client.dotenv_candidates", return_value=[tmp / ".env"]):
                config = LLMConfig.from_env(None)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(config.model, "dotenv-model")
        self.assertEqual(config.api_key, "dotenv-key")
        self.assertEqual(config.price_in_cny_per_mtok, 1.5)

    def test_real_environment_overrides_dotenv(self):
        from unittest.mock import patch

        tmp = Path(__import__("tempfile").mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / ".env").write_text(
            "INSIGHTFLOW_LLM_MODEL=dotenv-model\nDEEPSEEK_API_KEY=k\n"
            "INSIGHTFLOW_LLM_PRICE_IN=1\nINSIGHTFLOW_LLM_PRICE_OUT=1\n",
            encoding="utf-8",
        )
        import os

        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with patch("insightflow.llm.client.dotenv_candidates", return_value=[tmp / ".env"]), \
                 patch.dict(os.environ, {"INSIGHTFLOW_LLM_MODEL": "from-real-env"}, clear=False):
                config = LLMConfig.from_env(None)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(config.model, "from-real-env")


class EvaluateTests(unittest.TestCase):
    def test_role_accuracy(self):
        result = contracts.parse_result(json.dumps(VALID_ANSWER, ensure_ascii=False))
        report = evaluate.role_accuracy(
            result.fields, {"订单日期": "date", "区域": "dimension", "销售额": "metric"}
        )
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["correct"], 3)

        wrong = evaluate.role_accuracy(result.fields, {"销售额": "dimension"})
        self.assertEqual(wrong["accuracy"], 0.0)
        self.assertEqual(wrong["mismatches"][0]["got"], "metric")

    def test_business_number_detector(self):
        self.assertFalse(evaluate.contains_business_numbers(json.dumps(VALID_ANSWER, ensure_ascii=False)))
        self.assertTrue(evaluate.contains_business_numbers('{"analysis_plan": {"total": 42}}'))

    def test_summarize_runs(self):
        summary = evaluate.summarize_runs(
            [
                {"cost_cny": 1.2, "latency_ms": 1000, "role_accuracy": 1.0, "parse_ok": True, "business_numbers": False},
                {"cost_cny": 0.8, "latency_ms": 3000, "role_accuracy": 0.5, "parse_ok": False, "business_numbers": True},
            ]
        )
        self.assertEqual(summary["runs"], 2)
        self.assertAlmostEqual(summary["cost_total_cny"], 2.0)
        self.assertEqual(summary["latency_avg_ms"], 2000)
        self.assertEqual(summary["parse_failures"], 1)
        self.assertEqual(summary["fabricated_number_runs"], 1)


if __name__ == "__main__":
    unittest.main()
