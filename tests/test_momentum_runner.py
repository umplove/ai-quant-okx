import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.ai_reviewer import AiReview, AiTradeAttribution
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
        self.last_error = ""

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
        self.limit_buy_calls = []
        self.sell_calls = []
        self.cancel_stop_calls = []
        self.order_details = {}

    def get_market_tickers(self, inst_type="SPOT"):
        return self.tickers

    def get_balance(self, currency=None):
        return {"data": [{"totalEq": "10000"}]}

    def place_market_buy_quote(self, symbol, quote_amount, reason):
        self.buy_calls.append(symbol)
        request = OrderRequest(symbol, Side.BUY, quote_amount, "market", None, f"BUY{symbol}", reason, "quote_ccy")
        ok = self.buy_ok if isinstance(self.buy_ok, bool) else self.buy_ok.pop(0)
        result = OrderResult(
            ok,
            symbol,
            Side.BUY,
            "ord",
            request.client_order_id,
            {"avgPx": "1", "accFillSz": str(quote_amount)},
            None if ok else "All operations failed",
        )
        return request, result

    def place_limit_buy_quote(self, symbol, quote_amount, price, reason):
        self.limit_buy_calls.append((symbol, quote_amount, price))
        request = OrderRequest(symbol, Side.BUY, quote_amount / price, "limit", price, f"LBUY{symbol}{len(self.limit_buy_calls)}", reason)
        return request, OrderResult(True, symbol, Side.BUY, "limit-ord", request.client_order_id, {"avgPx": str(price)})

    def place_limit_sell_base(self, symbol, base_size, price, reason):
        request = OrderRequest(symbol, Side.SELL, base_size, "limit", price, f"LSELL{symbol}", reason)
        return request, OrderResult(True, symbol, Side.SELL, "limit-sell", request.client_order_id, {"avgPx": str(price)})

    def cancel_order(self, symbol, order_id):
        return {"code": "0", "data": [{"ordId": order_id}]}

    def list_open_orders(self, symbol=None):
        return {"code": "0", "data": []}

    def place_market_sell_base(self, symbol, base_size, reason):
        self.sell_calls.append(symbol)
        request = OrderRequest(symbol, Side.SELL, base_size, "market", None, f"SELL{symbol}{len(self.sell_calls)}", reason)
        return request, OrderResult(True, symbol, Side.SELL, "ord", request.client_order_id, {"avgPx": "2"})

    def place_stop_loss_order(self, symbol, size, stop_price):
        return StopLossOrder(symbol, "algo", f"SL{symbol}{len(self.buy_calls)}", stop_price, size, True, {})

    def cancel_stop_loss_order(self, symbol, algo_id):
        self.cancel_stop_calls.append((symbol, algo_id))
        return {"code": "0", "data": [{"algoId": algo_id}]}

    def get_order_details(self, symbol, order_id):
        return self.order_details.get(
            order_id,
            {"code": "0", "data": [{"ordId": order_id, "state": "live", "accFillSz": "0"}]},
        )


class _StopLossFailExchange(_TradingExchange):
    def place_stop_loss_order(self, symbol, size, stop_price):
        return StopLossOrder(symbol, None, f"SL{symbol}", stop_price, size, False, {}, "stop rejected")


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
            settings = _trading_settings(db, ("AAA-USDT", "BBB-USDT"), max_open_positions=2)
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
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("sell")
                client.return_value.decide_buy.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)

    def test_full_book_still_asks_ai_for_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 5, 1, 1))
            settings = _trading_settings(db, ("AAA-USDT", "BBB-USDT"), max_open_positions=1)
            settings = settings.__class__(**{**settings.__dict__, "ai_review_max_candidates": 2})
            exchange = _TradingExchange(
                [
                    MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1),
                    MarketTicker("BBB-USDT", 2, 1, 2, 1, 900, 1),
                ]
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("hold")
                client.return_value.decide_buy.return_value = _decision("buy")
                runner.run_once()

            self.assertEqual(client.return_value.decide_buy.call_count, 2)
            self.assertEqual(exchange.buy_calls, [])

    def test_stop_loss_failure_is_recorded_without_symbol_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _StopLossFailExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_buy.return_value = _decision("buy")
                client.return_value.decide_sell.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(storage.get_state("paused:AAA-USDT", ""), "")
            self.assertTrue(storage.get_position("AAA-USDT").is_open)
            self.assertTrue(storage.recent_trade_attributions())

    def test_money_report_is_manual_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            notifier = _Notifier()
            runner = MomentumBotRunner(settings, storage, _Exchange(), notifier)

            runner._send_money_report()
            self.assertEqual(notifier.messages, [])

            runner._send_money_report(force=True)
            self.assertEqual(len(notifier.messages), 1)
            self.assertIn("总资产", notifier.messages[0])

    def test_manual_ai_and_training_messages_are_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            runner = MomentumBotRunner(settings, storage, _Exchange(), _Notifier())

            self.assertIn("AI配置", runner._ai_status_message())
            self.assertIn("训练进度", runner._training_message())

    def test_limit_pullback_places_limit_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_buy.return_value = _decision("buy", entry_mode="limit_pullback")
                client.return_value.decide_sell.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.buy_calls, [])
            self.assertEqual(len(exchange.limit_buy_calls), 1)
            self.assertFalse(storage.get_position("AAA-USDT").is_open)

    def test_pending_limit_buy_blocks_duplicate_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            request = OrderRequest("AAA-USDT", Side.BUY, 5, "limit", 1.9, "LBUYAAA", "test")
            storage.save_order(
                request,
                OrderResult(True, "AAA-USDT", Side.BUY, "ord-1", "LBUYAAA", {"data": [{"ordId": "ord-1"}]}),
                status="pending",
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_buy.return_value = _decision("buy")
                client.return_value.decide_sell.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.buy_calls, [])
            self.assertEqual(exchange.limit_buy_calls, [])
            self.assertEqual(len(storage.pending_entry_orders("AAA-USDT")), 1)

    def test_filled_limit_buy_creates_position_and_stop_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            exchange.order_details["ord-1"] = {
                "code": "0",
                "data": [{"ordId": "ord-1", "state": "filled", "accFillSz": "5", "avgPx": "2"}],
            }
            request = OrderRequest("AAA-USDT", Side.BUY, 5, "limit", 2, "LBUYAAA", "test")
            storage.save_order(
                request,
                OrderResult(True, "AAA-USDT", Side.BUY, "ord-1", "LBUYAAA", {"data": [{"ordId": "ord-1"}]}),
                status="pending",
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_buy.return_value = _decision("buy")
                client.return_value.decide_sell.return_value = _decision("hold")
                runner.run_once()

            position = storage.get_position("AAA-USDT")
            self.assertTrue(position.is_open)
            self.assertEqual(position.base_qty, 5)
            self.assertEqual(position.avg_entry_price, 2)
            self.assertTrue(storage.active_stop_loss_orders("AAA-USDT"))

    def test_partial_fill_only_applies_incremental_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 2, 2, 2))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            exchange.order_details["ord-1"] = {
                "code": "0",
                "data": [{"ordId": "ord-1", "state": "partially_filled", "accFillSz": "3", "avgPx": "2"}],
            }
            request = OrderRequest("AAA-USDT", Side.BUY, 5, "limit", 2, "LBUYAAA", "test")
            storage.save_order(
                request,
                OrderResult(True, "AAA-USDT", Side.BUY, "ord-1", "LBUYAAA", {"data": [{"ordId": "ord-1"}]}),
                status="partial",
                filled_size=2,
                avg_fill_price=2,
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())
            scan = MomentumScan(tickers=exchange.tickers, info_signals=[], candidates=[])

            runner._sync_pending_entry_orders(scan)
            runner._sync_pending_entry_orders(scan)

            self.assertEqual(storage.get_position("AAA-USDT").base_qty, 3)

    def test_move_stop_replaces_existing_stop_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 5, 2, 2))
            storage.save_stop_loss_order(StopLossOrder("AAA-USDT", "algo-old", "SL-OLD", 1.6, 5, True, {}))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            runner._move_stop(storage.get_position("AAA-USDT"), 1.9, "test")

            self.assertEqual(exchange.cancel_stop_calls, [("AAA-USDT", "algo-old")])
            active = storage.active_stop_loss_orders("AAA-USDT")
            self.assertEqual(len(active), 1)
            self.assertAlmostEqual(active[0]["stop_price"], 1.9)

    def test_full_sell_cancels_active_stop_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 5, 1, 1))
            storage.save_stop_loss_order(StopLossOrder("AAA-USDT", "algo-old", "SL-OLD", 0.8, 5, True, {}))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            runner._sell_position(storage.get_position("AAA-USDT"), 2, "test")

            self.assertFalse(storage.get_position("AAA-USDT").is_open)
            self.assertEqual(exchange.cancel_stop_calls, [("AAA-USDT", "algo-old")])
            self.assertEqual(storage.active_stop_loss_orders("AAA-USDT"), [])

    def test_split_limit_places_multiple_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",))
            settings = settings.__class__(**{**settings.__dict__, "split_order_parts": 3})
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_buy.return_value = _decision("buy", entry_mode="split_limit")
                client.return_value.decide_sell.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(len(exchange.limit_buy_calls), 3)

    def test_partial_sell_keeps_remaining_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 10, 1, 1))
            settings = _trading_settings(db, ("AAA-USDT",))
            settings = settings.__class__(**{**settings.__dict__, "partial_sell_fractions": (0.3, 0.5, 1.0)})
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("sell", exit_mode="sell_partial")
                client.return_value.decide_buy.return_value = _decision("hold")
                client.return_value.attribute_trade.return_value = AiTradeAttribution(False, error="disabled")
                runner.run_once()

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertAlmostEqual(storage.get_position("AAA-USDT").base_qty, 7.0)

    def test_control_messages_for_execution_lessons_and_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_execution_decision("AAA-USDT", "buy", "buy", "market_now", "hold", "normal", "fixed", "none", 0.8, "测试")
            storage.save_trade_attribution("AAA-USDT", -1, -0.1, "追高", 0.8, "测试", "震荡")
            storage.save_market_regime("震荡", 0.8, "测试")
            settings = settings_for(db)
            runner = MomentumBotRunner(settings, storage, _Exchange(), _Notifier())

            self.assertIn("AI执行决策", runner._execution_message())
            self.assertIn("交易归因", runner._lessons_message())
            self.assertIn("AI行情状态", runner._market_message())


def _trading_settings(db: Path, symbols: tuple[str, ...], max_open_positions: int = 2):
    settings = settings_for(db)
    return settings.__class__(
        **{
            **settings.__dict__,
            "symbols": symbols,
            "trading_enabled": True,
            "okx_api_key": "k",
            "okx_secret_key": "s",
            "okx_passphrase": "p",
            "max_open_positions": max_open_positions,
            "news_scan_aggressive": False,
            "telegram_money_only": True,
            "ai_review_enabled": True,
            "openai_api_key": "test",
        }
    )


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


def _decision(
    action: str,
    entry_mode: str = "market_now",
    exit_mode: str = "sell_all",
    size_mode: str = "normal",
    stop_mode: str = "fixed",
    replace_mode: str = "none",
):
    from okx_quant_bot.ai_reviewer import AiTradeDecision

    return AiTradeDecision(
        True,
        action,
        0.9,
        "test",
        '{"action":"' + action + '"}',
        entry_mode=entry_mode,
        exit_mode=exit_mode,
        size_mode=size_mode,
        stop_mode=stop_mode,
        replace_mode=replace_mode,
    )


if __name__ == "__main__":
    unittest.main()
