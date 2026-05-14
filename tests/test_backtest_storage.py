import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.backtest import run_backtest
from okx_quant_bot.data import Storage
from okx_quant_bot.models import Candle, IntelligenceItem, Position, TradeReview
from okx_quant_bot.strategy import TrendPullbackStrategy


class BacktestStorageTests(unittest.TestCase):
    def test_storage_round_trips_candles(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "bot.sqlite3")
            storage.init()
            candles = [Candle("BTC-USDT", 1, 1, 2, 0.5, 1.5, 10)]
            storage.save_candles(candles)
            loaded = storage.load_candles("BTC-USDT")
            self.assertEqual(loaded, candles)

    def test_backtest_returns_metrics(self):
        closes = [100 + i * 0.2 for i in range(260)]
        candles = [
            Candle("BTC-USDT", idx, close, close + 1, close - 1, close, 1)
            for idx, close in enumerate(closes)
        ]
        strategy = TrendPullbackStrategy()
        result = run_backtest("BTC-USDT", candles, strategy)
        self.assertEqual(result.symbol, "BTC-USDT")
        self.assertGreaterEqual(result.ending_cash, 0)
        self.assertGreaterEqual(result.max_drawdown_pct, 0)

    def test_strategy_lessons_keep_winners(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "bot.sqlite3")
            storage.init()
            storage.save_strategy_lesson("BTC-USDT", 3.0, 0.3, "follow momentum")
            storage.save_strategy_lesson("ETH-USDT", -1.0, -0.1, "bad entry")

            lessons = storage.active_strategy_lessons()

        self.assertEqual(len(lessons), 1)
        self.assertIn("BTC-USDT", lessons[0])
        self.assertIn("follow momentum", lessons[0])

    def test_intelligence_and_trade_reviews_are_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "bot.sqlite3")
            storage.init()
            storage.save_intelligence_items(
                [IntelligenceItem("rss", "BTC-USDT", "Bitcoin listing", "u", 2.0)]
            )
            storage.save_trade_review(
                TradeReview("BTC-USDT", "mark", 100, 110, 1, 10, 10, "profit")
            )

            intel = storage.recent_intelligence()
            reviews = storage.recent_trade_reviews()

        self.assertIn("Bitcoin listing", intel[0])
        self.assertIn("BTC-USDT", reviews[0])

    def test_open_positions_returns_only_live_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "bot.sqlite3")
            storage.init()
            storage.save_position(Position("BTC-USDT", 1, 100, 100))
            storage.save_position(Position("ETH-USDT"))

            positions = storage.open_positions()

        self.assertEqual([p.symbol for p in positions], ["BTC-USDT"])

    def test_ai_call_audit_summary_records_cost_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "bot.sqlite3")
            storage.init()
            storage.save_ai_call_audit(
                "BTC-USDT", "buy", True, "buy", 0.9, 1000, 80, 250, "", "ok",
                prompt_tokens=100, completion_tokens=20, total_tokens=120, retry_count=1,
            )

            summary = storage.recent_ai_call_summary()

        self.assertIn("1/1", summary)
        self.assertIn("buy=1", summary)
        self.assertIn("token=120", summary)

    def test_training_and_shadow_summaries_are_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "bot.sqlite3")
            storage.init()
            storage.add_training_usage("2026-W20", 1_000_000_000, 100, 20, 120, True)
            storage.save_shadow_decision("BTC-USDT", "swap", "永续合约影子判断", "hold", 0.8, "风险一般")

            training = storage.training_summary("2026-W20", 1_000_000_000)
            shadows = storage.recent_shadow_decisions()

        self.assertIn("120/1000000000", training)
        self.assertIn("BTC-USDT", shadows[0])
        self.assertIn("swap", shadows[0])


if __name__ == "__main__":
    unittest.main()
