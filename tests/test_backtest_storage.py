import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.backtest import run_backtest
from okx_quant_bot.data import Storage
from okx_quant_bot.models import Candle
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


if __name__ == "__main__":
    unittest.main()

