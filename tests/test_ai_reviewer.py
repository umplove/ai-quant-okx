import json
import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.ai_reviewer import AiReviewClient, _extract_output_text
from okx_quant_bot.models import CandidateScore, InfoSignal, MarketTicker
from okx_quant_bot.momentum import MomentumScan
from tests.test_strategy_risk import settings_for


class AiReviewerTests(unittest.TestCase):
    def test_disabled_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            review = AiReviewClient(settings).review_scan(_scan(), open_position_count=0)

        self.assertFalse(review.ok)
        self.assertIn("disabled", review.error)

    def test_review_scan_posts_responses_request(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body["model"], "gpt-test")
            self.assertIn("BTC-USDT", body["input"])
            self.assertNotIn("secret-key", body["input"])
            return json.dumps({"output_text": "本轮建议继续观察。"}).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "openai_model": "gpt-test",
                }
            )
            review = AiReviewClient(settings, opener=opener).review_scan(_scan(), open_position_count=0)

        self.assertTrue(review.ok)
        self.assertEqual(review.text, "本轮建议继续观察。")
        self.assertEqual(len(calls), 1)
        auth = calls[0][0].headers["Authorization"]
        self.assertEqual(auth, "Bearer secret-key")

    def test_extracts_nested_output_text(self):
        payload = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": "第一段"},
                        {"type": "output_text", "text": "第二段"},
                    ]
                }
            ]
        }

        self.assertEqual(_extract_output_text(payload), "第一段\n第二段")


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
