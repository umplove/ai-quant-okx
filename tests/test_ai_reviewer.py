import json
import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.ai_reviewer import (
    AiReviewClient,
    _extract_ai_text,
    _extract_output_text,
    _parse_trade_decision,
)
from okx_quant_bot.models import CandidateScore, InfoSignal, MarketTicker
from okx_quant_bot.momentum import MomentumScan
from tests.test_strategy_risk import settings_for


class AiReviewerTests(unittest.TestCase):
    def test_disabled_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            review = AiReviewClient(settings).review_scan(_scan(), open_position_count=0)

        self.assertFalse(review.ok)
        self.assertIn("AI未启用", review.error)

    def test_mimo_chat_request_body_and_usage(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body["model"], "mimo-v2.5-pro")
            self.assertIn("messages", body)
            self.assertIn("BTC-USDT", body["messages"][1]["content"])
            self.assertNotIn("secret-key", body["messages"][1]["content"])
            self.assertEqual(body["max_completion_tokens"], 1024)
            self.assertEqual(body["thinking"], {"type": "disabled"})
            return json.dumps(
                {
                    "choices": [{"message": {"content": "资金风险可控，继续按风控运行。"}}],
                    "usage": {"prompt_tokens": 57, "completion_tokens": 72, "total_tokens": 129},
                },
                ensure_ascii=False,
            ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "openai_model": "mimo-v2.5-pro",
                    "openai_base_url": "https://api.xiaomimimo.com/v1",
                    "openai_api_mode": "chat",
                    "ai_review_max_tokens": 1024,
                }
            )
            review = AiReviewClient(settings, opener=opener).review_scan(_scan(), open_position_count=0)

        self.assertTrue(review.ok)
        self.assertEqual(review.text, "资金风险可控，继续按风控运行。")
        self.assertEqual(review.total_tokens, 129)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].headers["Authorization"], "Bearer secret-key")
        self.assertTrue(calls[0][0].full_url.endswith("/chat/completions"))

    def test_responses_mode_still_supported(self):
        def opener(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body["model"], "gpt-test")
            self.assertIn("BTC-USDT", body["input"])
            return json.dumps({"output_text": "本轮建议继续观察。"}, ensure_ascii=False).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "openai_model": "gpt-test",
                    "openai_base_url": "https://api.openai.com/v1",
                    "openai_api_mode": "responses",
                }
            )
            review = AiReviewClient(settings, opener=opener).review_scan(_scan(), open_position_count=0)

        self.assertTrue(review.ok)
        self.assertEqual(review.text, "本轮建议继续观察。")

    def test_timeout_retries_then_succeeds(self):
        calls = []

        def opener(request, timeout):
            calls.append(timeout)
            if len(calls) < 3:
                raise TimeoutError()
            return json.dumps(
                {
                    "choices": [{"message": {"content": '{"action":"hold","confidence":0.8,"reason":"重试后成功"}'}}],
                    "usage": {"total_tokens": 10},
                },
                ensure_ascii=False,
            ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "ai_review_timeout_seconds": 3.0,
                    "ai_request_retries": 2,
                    "ai_retry_backoff_seconds": 0.0,
                }
            )
            decision = AiReviewClient(settings, opener=opener).decide_buy(_scan(), _scan().candidates[0], 0)

        self.assertTrue(decision.ok)
        self.assertEqual(decision.retry_count, 2)
        self.assertEqual(len(calls), 3)

    def test_timeout_is_short_error_after_retries(self):
        def opener(request, timeout):
            self.assertEqual(timeout, 3.0)
            raise TimeoutError()

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "ai_review_timeout_seconds": 3.0,
                    "ai_request_retries": 1,
                    "ai_retry_backoff_seconds": 0.0,
                }
            )
            review = AiReviewClient(settings, opener=opener).review_scan(_scan(), 0)

        self.assertFalse(review.ok)
        self.assertEqual(review.error, "timeout")
        self.assertEqual(review.retry_count, 1)

    def test_market_regime_bad_json_is_repaired_once(self):
        responses = [
            "regime=震荡 confidence=0.7 reason=原始格式坏了",
            '{"regime":"震荡","confidence":0.7,"reason":"修复成功"}',
        ]

        def opener(request, timeout):
            return json.dumps({"choices": [{"message": {"content": responses.pop(0)}}]}, ensure_ascii=False).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "ai_request_retries": 0,
                }
            )
            regime = AiReviewClient(settings, opener=opener).decide_market_regime(_scan())

        self.assertTrue(regime.ok)
        self.assertEqual(regime.regime, "震荡")
        self.assertEqual(regime.reason, "修复成功")

    def test_training_bad_json_failure_is_classified(self):
        def opener(request, timeout):
            return json.dumps({"choices": [{"message": {"content": "不是JSON"}}]}, ensure_ascii=False).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "ai_request_retries": 0,
                }
            )
            decision = AiReviewClient(settings, opener=opener).complete_training("训练")

        self.assertFalse(decision.ok)
        self.assertIn("training_parse_failed", decision.error)

    def test_extracts_nested_output_text(self):
        payload = {"output": [{"content": [{"text": "第一段"}, {"text": "第二段"}]}]}

        self.assertEqual(_extract_output_text(payload), "第一段\n第二段")

    def test_extracts_chat_completion_text(self):
        payload = {"choices": [{"message": {"content": "继续运行"}}]}

        self.assertEqual(_extract_ai_text(payload), "继续运行")

    def test_summarizes_reasoning_when_chat_content_is_empty(self):
        payload = {"choices": [{"message": {"content": "", "reasoning_content": "风险低，可以允许规则策略继续运行。"}}]}

        self.assertIn('"action":"buy"', _extract_ai_text(payload))

    def test_parse_trade_decision_json(self):
        decision = _parse_trade_decision(
            '{"action":"sell","exit_mode":"sell_partial","confidence":0.8,"reason":"跌破风险线"}'
        )

        self.assertTrue(decision.approved_sell)
        self.assertEqual(decision.exit_mode, "sell_partial")
        self.assertEqual(decision.reason, "跌破风险线")

    def test_parse_trade_decision_execution_defaults(self):
        decision = _parse_trade_decision(
            '{"action":"buy","entry_mode":"split_limit","size_mode":"strong","stop_mode":"trailing",'
            '"replace_mode":"replace_weakest","confidence":0.9,"reason":"强势突破"}'
        )

        self.assertTrue(decision.approved_buy)
        self.assertEqual(decision.entry_mode, "split_limit")
        self.assertEqual(decision.size_mode, "strong")
        self.assertEqual(decision.stop_mode, "trailing")
        self.assertEqual(decision.replace_mode, "replace_weakest")

    def test_invalid_execution_modes_fall_back_safely(self):
        decision = _parse_trade_decision(
            '{"action":"buy","entry_mode":"bad","size_mode":"bad","stop_mode":"bad",'
            '"replace_mode":"bad","confidence":0.9,"reason":"测试"}'
        )

        self.assertEqual(decision.entry_mode, "market_now")
        self.assertEqual(decision.size_mode, "normal")
        self.assertEqual(decision.stop_mode, "fixed")
        self.assertEqual(decision.replace_mode, "none")


def _scan() -> MomentumScan:
    ticker = MarketTicker("BTC-USDT", 110, 100, 120, 95, 1000000, 1)
    candidate = CandidateScore(
        symbol="BTC-USDT",
        price=110,
        change_pct_24h=0.10,
        amplitude_pct_24h=0.25,
        volume_quote_24h=1000000,
        news_score=1,
        polymarket_score=0,
        total_score=30,
        reason="news confirmed",
        confirmed=True,
    )
    signal = InfoSignal("news", "BTC-USDT", 1, "Bitcoin breaks higher")
    return MomentumScan(tickers=[ticker], info_signals=[signal], candidates=[candidate])


if __name__ == "__main__":
    unittest.main()
