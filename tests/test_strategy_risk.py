import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.models import Candle, Position, Signal, SignalAction
from okx_quant_bot.risk import RiskManager
from okx_quant_bot.strategy import TrendPullbackStrategy


def settings_for(db_path: Path) -> Settings:
    return Settings(
        okx_api_key="",
        okx_secret_key="",
        okx_passphrase="",
        okx_demo=True,
        okx_base_url="https://www.okx.com",
        simulated_trading_header=True,
        trading_enabled=False,
        allow_live_trading=False,
        symbols=("BTC-USDT", "ETH-USDT"),
        bar="1H",
        db_path=db_path,
        ema_fast=3,
        ema_slow=5,
        rsi_period=3,
        rsi_low=35,
        max_trade_fraction=0.10,
        max_symbol_fraction=0.20,
        stop_loss_pct=0.015,
        take_profit_pct=0.03,
        trailing_stop_pct=0.01,
        max_daily_loss_pct=0.03,
        max_consecutive_losses=3,
        telegram_bot_token="",
        telegram_chat_id="",
        risk_halt_enabled=True,
    )


class StrategyRiskTests(unittest.TestCase):
    def test_strategy_sells_on_stop_loss(self):
        candles = [
            Candle("BTC-USDT", i, 100, 101, 90, close, 1)
            for i, close in enumerate([100, 101, 102, 103, 104, 98])
        ]
        strategy = TrendPullbackStrategy(ema_fast=3, ema_slow=5, rsi_period=3)
        signal = strategy.generate(candles, Position("BTC-USDT", 1, 100, 105))
        self.assertEqual(signal.action, SignalAction.SELL)
        self.assertEqual(signal.reason, "stop_loss")

    def test_risk_blocks_after_consecutive_losses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            risk = RiskManager(settings, storage)
            for _ in range(3):
                risk.record_trade_pnl(-1)
            signal = Signal("BTC-USDT", 1, SignalAction.BUY, 100, "test")
            decision = risk.can_open_position(signal, 1000, 1000, Position("BTC-USDT"))
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "max_consecutive_losses_reached")

    def test_risk_blocks_daily_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            risk = RiskManager(settings, storage)
            signal = Signal("BTC-USDT", 1, SignalAction.BUY, 100, "test")
            risk.can_open_position(signal, 1000, 1000, Position("BTC-USDT"))
            decision = risk.can_open_position(signal, 950, 950, Position("BTC-USDT"))
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "max_daily_loss_reached")

    def test_training_mode_does_not_halt_after_losses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db).__class__(
                **{**settings_for(db).__dict__, "risk_halt_enabled": False}
            )
            risk = RiskManager(settings, storage)
            for _ in range(10):
                risk.record_trade_pnl(-1)
            signal = Signal("BTC-USDT", 1, SignalAction.BUY, 100, "test")

            decision = risk.can_open_position(signal, 1000, 1000, Position("BTC-USDT"))

            self.assertTrue(decision.allowed)
            self.assertEqual(decision.reason, "risk_ok")


if __name__ == "__main__":
    unittest.main()
