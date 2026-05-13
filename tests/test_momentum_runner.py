import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from okx_quant_bot.ai_reviewer import AiReview
from okx_quant_bot.data import Storage
from okx_quant_bot.models import CandidateScore, MarketTicker, OrderRequest, OrderResult, Position, Side, StopLossOrder
from okx_quant_bot.momentum import MomentumScan
from okx_quant_bot.momentum_runner import MomentumBotRunner
from tests.test_strategy_risk import settings_for


class _Exchange:
    pass


class _Notifier:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)

    def send_money(self, message):
        self.messages.append(message)

    def poll_controls(self, storage):
        return []

    def setup_commands(self):
        pass


class _TradingExchange:
    def __init__(self, tickers, buy_ok=True):
        self.tickers = tickers
        self.buy_ok = buy_ok
        self.buy_calls = []
        self.sell_calls = []

    def get_market_tickers(self, inst_type="SPOT"):
        return self.tickers

    def get_balance(self, currency=None):
        return {"data": [{"totalEq": "10000"}]}

    def place_market_buy_quote(self, symbol, quote_amount, reason):
        self.buy_calls.append(symbol)
        request = OrderRequest(symbol, Side.BUY, quote_amount, "market", None, f"BUY{symbol}", reason, "quote_ccy")
        ok = self.buy_ok if isinstance(self.buy_ok, bool) else self.buy_ok.pop(0)
        result = OrderResult(ok, symbol, Side.BUY, "ord", request.client_order_id, {"avgPx": "1", "accFillSz": str(quote_amount)}, None if ok else "All operations failed")
        return request, result

    def place_market_sell_base(self, symbol, base_size, reason):
        self.sell_calls.append(symbol)
        request = OrderRequest(symbol, Side.SELL, base_size, "market", None, f"SELL{symbol}", reason)
        return request, OrderResult(True, symbol, Side.SELL, "ord", request.client_order_id, {"avgPx": "2"})

    def place_stop_loss_order(self, symbol, size, stop_price):
        return StopLossOrder(symbol, "algo", f"SL{symbol}", stop_price, size, True, {})


class MomentumRunnerTests(unittest.TestCase):
    def test_tradable_candidates_fill_available_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "max_open_positions": 3,
                    "symbols": ("AAA-USDT", "BBB-USDT", "CCC-USDT", "DDD-USDT"),
                }
            )
            runner = MomentumBotRunner(settings, storage, _Exchange(), _Notifier())
            scan = MomentumScan(
                tickers=[],
                info_signals=[],
                candidates=[
                    _candidate("AAA-USDT"),
                    _candidate("BBB-USDT"),
                    _candidate("CCC-USDT", confirmed=False),
                    _candidate("DDD-USDT"),
                ],
            )

            candidates = runner._tradable_candidates(scan)

        self.assertEqual([c.symbol for c in candidates], ["AAA-USDT", "BBB-USDT", "DDD-USDT"])

    def test_ai_timeout_is_silent_in_money_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "ai_review_enabled": True,
                    "ai_review_interval_scans": 1,
                    "telegram_money_only": True,
                }
            )
            runner = MomentumBotRunner(settings, storage, _Exchange(), _Notifier())
            scan = MomentumScan(tickers=[], info_signals=[], candidates=[])

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.review_scan.return_value = AiReview(False, "", "timeout")
                text = runner._ai_review_text(scan)

        self.assertEqual(text, "")

    def test_ai_buy_failure_continues_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "symbols": ("AAA-USDT", "BBB-USDT"),
                    "trading_enabled": True,
                    "okx_api_key": "k",
                    "okx_secret_key": "s",
                    "okx_passphrase": "p",
                    "max_open_positions": 2,
                    "news_scan_aggressive": False,
                    "telegram_money_only": True,
                }
            )
            exchange = _TradingExchange(
                [
                    MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1),
                    MarketTicker("BBB-USDT", 2, 1, 2, 1, 900, 1),
                ],
                buy_ok=[False, True],
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_buy.return_value = _decision("buy")
                client.return_value.decide_sell.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.buy_calls, ["AAA-USDT", "BBB-USDT"])
            self.assertTrue(storage.get_position("BBB-USDT").is_open)

    def test_ai_sell_closes_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 5, 1, 1))
            settings = settings_for(db)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "symbols": ("AAA-USDT",),
                    "trading_enabled": True,
                    "okx_api_key": "k",
                    "okx_secret_key": "s",
                    "okx_passphrase": "p",
                    "news_scan_aggressive": False,
                    "telegram_money_only": True,
                }
            )
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("sell")
                client.return_value.decide_buy.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)


def _candidate(symbol: str, confirmed: bool = True) -> CandidateScore:
    return CandidateScore(
        symbol=symbol,
        price=1,
        change_pct_24h=0.1,
        amplitude_pct_24h=0.2,
        volume_quote_24h=1000,
        news_score=0,
        polymarket_score=0,
        total_score=10,
        reason="test",
        confirmed=confirmed,
    )


def _decision(action: str):
    from okx_quant_bot.ai_reviewer import AiTradeDecision

    return AiTradeDecision(True, action, 0.9, "test", '{"action":"' + action + '"}')


if __name__ == "__main__":
    unittest.main()
