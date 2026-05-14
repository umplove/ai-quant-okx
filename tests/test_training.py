import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.ai_reviewer import AiTradeDecision
from okx_quant_bot.data import Storage
from okx_quant_bot.models import CandidateScore, MarketTicker
from okx_quant_bot.momentum import MomentumScan
from okx_quant_bot.training import AiTrainingPool, current_week_key
from tests.test_strategy_risk import settings_for


class TrainingPoolTests(unittest.TestCase):
    def test_training_pool_records_usage_and_shadow_decisions(self):
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
                pool.enqueue_scan(scan, "经验")
                pool._queue.join()

            self.assertIn("1080", storage.training_summary(current_week_key(), settings.ai_weekly_token_target))
            self.assertTrue(storage.recent_shadow_decisions())


if __name__ == "__main__":
    unittest.main()
