import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.ai_reviewer import AiTradeDecision
from okx_quant_bot.data import Storage
from okx_quant_bot.models import CandidateScore, MarketTicker, Position
from okx_quant_bot.momentum import MomentumScan
from okx_quant_bot.training import AiTrainingPool, current_week_key
from tests.test_strategy_risk import settings_for


class TrainingPoolTests(unittest.TestCase):
    def test_training_pool_records_real_portfolio_usage_without_shadow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "ai_training_workers": 1,
                    "ai_review_max_candidates": 1,
                }
            )
            scan = MomentumScan(
                tickers=[MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)],
                info_signals=[],
                candidates=[
                    CandidateScore("AAA-USDT", 2, 1, 1, 1000, 0, 0, 10, "test", True),
                ],
            )

            with patch("okx_quant_bot.training.AiReviewClient") as client:
                client.return_value.complete_training.return_value = AiTradeDecision(
                    True,
                    "hold",
                    0.8,
                    "影子训练成功",
                    '{"action":"hold","confidence":0.8,"reason":"影子训练成功"}',
                    prompt_tokens=100,
                    completion_tokens=20,
                    total_tokens=120,
                )
                pool = AiTrainingPool(settings, storage)
                pool.start()
                pool.enqueue_scan(scan, "经验", [Position("AAA-USDT", 1, 2, 2)])
                pool._queue.join()
                pool.stop()

            self.assertIn("120", storage.training_summary(current_week_key(), settings.ai_weekly_token_target))
            self.assertIn("经验入库=1/1", storage.training_summary(current_week_key(), settings.ai_weekly_token_target))
            self.assertFalse(storage.recent_shadow_decisions())
            self.assertIn("portfolio_training", storage.recent_ai_call_breakdown())
            self.assertIn("portfolio_training", storage.recent_real_experiences()[0])

    def test_worker_exception_is_recorded_and_thread_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "openai_api_key": "secret-key",
                    "ai_training_workers": 1,
                    "ai_review_max_candidates": 1,
                }
            )
            scan = MomentumScan(
                tickers=[MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)],
                info_signals=[],
                candidates=[CandidateScore("AAA-USDT", 2, 1, 1, 1000, 0, 0, 10, "test", True)],
            )
            calls = {"count": 0}

            def complete_training(prompt):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("boom")
                return AiTradeDecision(
                    True,
                    "hold",
                    0.8,
                    "训练成功",
                    '{"action":"hold","confidence":0.8,"reason":"训练成功"}',
                    prompt_tokens=10,
                    completion_tokens=2,
                    total_tokens=12,
                    attempted_tokens=12,
                )

            with patch("okx_quant_bot.training.AiReviewClient") as client:
                client.return_value.complete_training.side_effect = complete_training
                pool = AiTrainingPool(settings, storage)
                pool.start()
                pool.enqueue_scan(scan, "经验", [Position("AAA-USDT", 1, 2, 2), Position("BBB-USDT", 1, 2, 2)])
                pool._queue.join()

            status = pool.status()
            self.assertEqual(status["alive_threads"], 1)
            self.assertGreaterEqual(status["worker_errors"], 1)
            self.assertTrue(storage.recent_bot_errors())
            self.assertIn("成功", storage.training_summary(current_week_key(), settings.ai_weekly_token_target))
            pool.stop()


if __name__ == "__main__":
    unittest.main()
