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
    def __init__(self, actions=None):
        self.messages = []
        self.last_error = ""
        self.actions = list(actions or [])

    def send(self, message):
        self.messages.append(message)

    def send_money(self, message):
        self.messages.append(message)

    def poll_controls(self, storage):
        actions, self.actions = self.actions, []
        return actions

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

    def place_margin_market(self, symbol, side, base_size, direction, leverage, margin_mode, reason, reduce_only=False):
        if reduce_only:
            self.sell_calls.append(symbol)
        else:
            self.buy_calls.append(symbol)
        request = OrderRequest(
            symbol,
            side,
            base_size,
            "market",
            None,
            f"MG{symbol}{len(self.buy_calls) + len(self.sell_calls)}",
            reason,
            market_type="MARGIN",
            td_mode=margin_mode,
            reduce_only=reduce_only,
            leverage=leverage,
            direction=direction,
        )
        return request, OrderResult(True, symbol, side, "ord", request.client_order_id, {"avgPx": "2", "accFillSz": str(base_size)})

    def place_swap_market(self, symbol, side, contract_size, direction, leverage, margin_mode, reason, reduce_only=False):
        if reduce_only:
            self.sell_calls.append(symbol)
        else:
            self.buy_calls.append(symbol)
        request = OrderRequest(
            symbol,
            side,
            contract_size,
            "market",
            None,
            f"SW{symbol}{len(self.buy_calls) + len(self.sell_calls)}",
            reason,
            market_type="SWAP",
            td_mode=margin_mode,
            pos_side=direction,
            reduce_only=reduce_only,
            leverage=leverage,
            direction=direction,
        )
        return request, OrderResult(True, symbol, side, "ord", request.client_order_id, {"avgPx": "2", "accFillSz": str(contract_size)})

    def swap_contract_size_for_quote(self, symbol, quote_amount, price):
        return quote_amount / price if price > 0 else 0

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


class _SyncExchange(_TradingExchange):
    def __init__(self, tickers, details=None, positions=None, open_orders=None):
        super().__init__(tickers)
        self.details = details or []
        self.positions = positions or []
        self.open_orders = open_orders or []

    def get_balance(self, currency=None):
        return {"data": [{"totalEq": "95106.17", "details": self.details}]}

    def get_positions(self, inst_type=None, symbol=None):
        rows = [
            row
            for row in self.positions
            if (not inst_type or row.get("instType") == inst_type)
            and (not symbol or row.get("instId") == symbol)
        ]
        return {"code": "0", "data": rows}

    def list_open_orders(self, symbol=None, inst_type="SPOT"):
        rows = [row for row in self.open_orders if row.get("instType", "SPOT") == inst_type]
        return {"code": "0", "data": rows}


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
            storage.save_position(Position("AAA-USDT", 5, 2, 2))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("sell")
                client.return_value.decide_buy.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)

    def test_full_book_skips_buy_ai_and_manages_positions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 5, 2, 2))
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

            self.assertEqual(client.return_value.decide_buy.call_count, 0)
            self.assertIn("持仓已满", storage.get_state("last_buy_ai_skip_reason"))
            self.assertEqual(exchange.buy_calls, [])

    def test_okx_sync_replaces_local_positions_without_auto_alert_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            symbols = tuple(f"AAA{i}-USDT" for i in range(7))
            tickers = [MarketTicker(symbol, 2, 1, 2, 1, 1000, 1) for symbol in symbols]
            details = [
                {"ccy": symbol.split("-")[0], "eq": "1", "eqUsd": "2", "availBal": "1"}
                for symbol in symbols
            ]
            settings = _trading_settings(db, symbols, max_open_positions=10)
            exchange = _SyncExchange(tickers, details=details)
            notifier = _Notifier()
            runner = MomentumBotRunner(settings, storage, exchange, notifier)
            scan = MomentumScan(tickers=tickers, info_signals=[], candidates=[])

            runner._sync_exchange_state(scan)

            self.assertEqual(storage.open_position_count(), 7)
            self.assertEqual(storage.get_state("okx_last_position_count"), "7")
            self.assertEqual(notifier.messages, [])
            self.assertIn("OKX同步正常", storage.get_state("okx_sync_status"))

    def test_okx_sync_merges_duplicate_okx_symbols_before_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            symbols = ("AAA-USDT", "BBB-USDT")
            tickers = [
                MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1),
                MarketTicker("BBB-USDT", 4, 1, 4, 1, 1000, 1),
            ]
            details = [
                {"ccy": "AAA", "eq": "1", "eqUsd": "2", "availBal": "1"},
                {"ccy": "AAA", "eq": "2", "eqUsd": "4", "availBal": "2"},
                {"ccy": "BBB", "eq": "1", "eqUsd": "4", "availBal": "1"},
            ]
            settings = _trading_settings(db, symbols, max_open_positions=10)
            exchange = _SyncExchange(tickers, details=details)
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())
            scan = MomentumScan(tickers=tickers, info_signals=[], candidates=[])

            runner._sync_exchange_state(scan)

            self.assertEqual(storage.open_position_count(), 2)
            self.assertEqual(storage.get_state("okx_last_position_count"), "2")
            self.assertEqual(storage.get_state("okx_raw_position_count"), "3")
            self.assertEqual(storage.get_position("AAA-USDT").base_qty, 3)
            self.assertIn("合并重复=1", storage.get_state("okx_sync_status"))

    def test_buy_ai_candidates_are_capped_by_available_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            symbols = tuple(f"AAA{i}-USDT" for i in range(12))
            for symbol in symbols[:7]:
                storage.save_position(Position(symbol, 1, 2, 2))
            settings = _trading_settings(db, symbols, max_open_positions=10)
            settings = settings.__class__(**{**settings.__dict__, "ai_review_max_candidates": 10})
            candidates = [
                CandidateScore(symbol, 2, 1, 1, 1000, 0, 0, 10 - idx, "test", True)
                for idx, symbol in enumerate(symbols)
            ]
            scan = MomentumScan(tickers=[], info_signals=[], candidates=candidates)
            runner = MomentumBotRunner(settings, storage, _TradingExchange([]), _Notifier())

            selected = runner._ai_learning_candidates(scan)

            self.assertEqual(len(selected), 4)
            self.assertIn("最多审核4", storage.get_state("last_buy_ai_skip_reason"))

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
            self.assertIn("OKX总权益", notifier.messages[0])

    def test_sleep_polls_manual_status_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = settings_for(db)
            notifier = _Notifier(actions=["status"])
            runner = MomentumBotRunner(settings, storage, _Exchange(), notifier)

            with patch("okx_quant_bot.momentum_runner.time.sleep"):
                runner._sleep_with_controls(0.1)

            self.assertTrue(notifier.messages)
            self.assertIn("OKX总权益", notifier.messages[0])
            self.assertEqual(storage.get_state("runtime_stage"), "sleep")

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
            settings = settings.__class__(**{**settings.__dict__, "momentum_exit_guard_enabled": False})
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

    def test_hard_take_profit_sells_full_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 10, 100, 100))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 103, 100, 103, 99, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())
            scan = MomentumScan(tickers=exchange.tickers, info_signals=[], candidates=[])

            runner._sell_positions_with_hard_exit(scan)

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)
            self.assertIn("hard_take_profit", storage.recent_strategy_lessons()[0])

    def test_hard_stop_loss_sells_even_when_ai_would_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 10, 100, 100))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 98, 97, 99, 96, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("hold")
                client.return_value.decide_buy.return_value = _decision("hold")
                runner.run_once()

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)
            self.assertIn("hard_stop_loss", storage.recent_strategy_lessons()[0])

    def test_hard_trailing_stop_sells_after_pullback_from_high(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 10, 100, 102))
            settings = _trading_settings(db, ("AAA-USDT",))
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 100.9, 100, 102, 99, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())
            scan = MomentumScan(tickers=exchange.tickers, info_signals=[], candidates=[])

            runner._sell_positions_with_hard_exit(scan)

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)
            self.assertIn("hard_trailing_stop", storage.recent_strategy_lessons()[0])

    def test_existing_positions_can_fill_remaining_slots_to_five(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 1, 2, 2))
            storage.save_position(Position("BBB-USDT", 1, 2, 2))
            symbols = ("AAA-USDT", "BBB-USDT", "CCC-USDT", "DDD-USDT", "EEE-USDT", "FFF-USDT")
            settings = _trading_settings(db, symbols, max_open_positions=5)
            settings = settings.__class__(**{**settings.__dict__, "ai_review_max_candidates": 6})
            exchange = _TradingExchange(
                [
                    MarketTicker("AAA-USDT", 2, 2, 2, 1, 1000, 1),
                    MarketTicker("BBB-USDT", 2, 2, 2, 1, 1000, 1),
                    MarketTicker("CCC-USDT", 2, 1, 2, 1, 1000, 1),
                    MarketTicker("DDD-USDT", 2, 1, 2, 1, 900, 1),
                    MarketTicker("EEE-USDT", 2, 1, 2, 1, 800, 1),
                    MarketTicker("FFF-USDT", 2, 1, 2, 1, 700, 1),
                ]
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("hold")
                client.return_value.decide_buy.return_value = _decision("buy")
                runner.run_once()

            self.assertEqual(exchange.buy_calls, ["CCC-USDT", "DDD-USDT", "EEE-USDT"])
            self.assertEqual(storage.open_position_count(), 5)

    def test_pending_entry_is_skipped_but_other_symbols_can_fill_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 1, 2, 2))
            storage.save_position(Position("BBB-USDT", 1, 2, 2))
            request = OrderRequest("CCC-USDT", Side.BUY, 5, "limit", 2, "LBUYCCC", "test")
            storage.save_order(
                request,
                OrderResult(True, "CCC-USDT", Side.BUY, "ord-1", "LBUYCCC", {"data": [{"ordId": "ord-1"}]}),
                status="pending",
            )
            symbols = ("AAA-USDT", "BBB-USDT", "CCC-USDT", "DDD-USDT", "EEE-USDT", "FFF-USDT")
            settings = _trading_settings(db, symbols, max_open_positions=5)
            settings = settings.__class__(**{**settings.__dict__, "ai_review_max_candidates": 6})
            exchange = _TradingExchange(
                [
                    MarketTicker("AAA-USDT", 2, 2, 2, 1, 1000, 1),
                    MarketTicker("BBB-USDT", 2, 2, 2, 1, 1000, 1),
                    MarketTicker("CCC-USDT", 2, 1, 2, 1, 1000, 1),
                    MarketTicker("DDD-USDT", 2, 1, 2, 1, 900, 1),
                    MarketTicker("EEE-USDT", 2, 1, 2, 1, 800, 1),
                    MarketTicker("FFF-USDT", 2, 1, 2, 1, 700, 1),
                ]
            )
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            with patch("okx_quant_bot.momentum_runner.AiReviewClient") as client:
                client.return_value.decide_sell.return_value = _decision("hold")
                client.return_value.decide_buy.return_value = _decision("buy")
                runner.run_once()

            self.assertEqual(exchange.buy_calls, ["DDD-USDT", "EEE-USDT", "FFF-USDT"])
            self.assertEqual(len(storage.pending_entry_orders("CCC-USDT")), 1)

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


    def test_rules_first_buy_does_not_require_ai_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",), max_open_positions=1)
            settings = settings.__class__(**{**settings.__dict__, "momentum_entry_mode": "rules_first"})
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 2, 1, 2, 1, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())

            decision = runner._entry_decision(_candidate("AAA-USDT"), {"action": "hold", "confidence": 0.5, "reason": "too early"})
            runner._execute_buy_decision(_candidate("AAA-USDT"), decision, MomentumScan(exchange.tickers, [], []))

            self.assertEqual(exchange.buy_calls, ["AAA-USDT"])
            self.assertTrue(storage.get_position("AAA-USDT").is_open)

    def test_ai_high_confidence_hold_vetoes_rules_first_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",), max_open_positions=1)
            settings = settings.__class__(**{**settings.__dict__, "momentum_entry_mode": "rules_first"})
            runner = MomentumBotRunner(settings, storage, _TradingExchange([]), _Notifier())

            decision = runner._entry_decision(_candidate("AAA-USDT"), {"action": "hold", "confidence": 0.95, "reason": "risk"})

            self.assertIsNone(decision)
            self.assertIn("ai_veto_buy", storage.recent_strategy_lessons()[0])

    def test_swap_short_entry_and_reduce_only_exit_use_derivative_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            settings = _trading_settings(db, ("AAA-USDT",), max_open_positions=2)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "enabled_market_types": ("SWAP",),
                    "allow_derivatives_trading": True,
                    "max_leverage": 5,
                    "margin_mode": "isolated",
                }
            )
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 1.8, 2, 2.1, 1.7, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())
            candidate = _candidate("AAA-USDT")
            candidate = candidate.__class__(**{**candidate.__dict__, "price": 1.8, "change_pct_24h": -0.1})

            runner._open_candidate_position(candidate, 100, "test")
            position = storage.get_position("AAA-USDT-SWAP")
            runner._sell_position(position, 1.7, "test")

            self.assertEqual(position.market_type, "SWAP")
            self.assertEqual(position.direction, "short")
            self.assertEqual(position.leverage, 5)
            self.assertFalse(storage.get_position("AAA-USDT-SWAP").is_open)

    def test_max_hold_time_triggers_time_rotation_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bot.sqlite3"
            storage = Storage(db)
            storage.init()
            storage.save_position(Position("AAA-USDT", 10, 100, 100))
            with storage.session() as conn:
                conn.execute("update positions set opened_at = datetime('now', '-10 minutes') where symbol = 'AAA-USDT'")
            settings = _trading_settings(db, ("AAA-USDT",))
            settings = settings.__class__(**{**settings.__dict__, "momentum_max_hold_minutes": 5})
            exchange = _TradingExchange([MarketTicker("AAA-USDT", 100.5, 100, 101, 99, 1000, 1)])
            runner = MomentumBotRunner(settings, storage, exchange, _Notifier())
            scan = MomentumScan(tickers=exchange.tickers, info_signals=[], candidates=[])

            runner._sell_positions_with_hard_exit(scan)

            self.assertEqual(exchange.sell_calls, ["AAA-USDT"])
            self.assertFalse(storage.get_position("AAA-USDT").is_open)


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
