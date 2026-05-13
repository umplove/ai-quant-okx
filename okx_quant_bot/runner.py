from __future__ import annotations

import time
from dataclasses import dataclass

from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.models import OrderRequest, OrderResult, Side, SignalAction
from okx_quant_bot.notify import Notifier
from okx_quant_bot.risk import RiskManager
from okx_quant_bot.strategy import TrendPullbackStrategy


@dataclass
class BotRunner:
    settings: Settings
    storage: Storage
    exchange: OkxRestClient
    notifier: Notifier

    def run_forever(self) -> None:
        self.settings.require_safe_trading_config()
        self.storage.init()
        self.notifier.send(_start_message(self.settings.okx_demo))
        strategy = self._strategy()
        risk = RiskManager(self.settings, self.storage)
        while True:
            for symbol in self.settings.symbols:
                try:
                    self.run_once(symbol, strategy, risk)
                except Exception as exc:
                    risk.pause_symbol(symbol, f"runtime_error:{exc}")
                    self.notifier.send(_runtime_error_message(symbol, exc))
            time.sleep(60)

    def run_once(self, symbol: str, strategy: TrendPullbackStrategy, risk: RiskManager) -> None:
        candles = self.exchange.get_candles(symbol, self.settings.bar, limit=max(self.settings.ema_slow + 5, 250))
        self.storage.save_candles(candles)
        position = self.storage.get_position(symbol)
        if position.is_open and candles:
            position.highest_price = max(position.highest_price, candles[-1].close)
            self.storage.save_position(position)
        signal = strategy.generate(candles, position)
        self.storage.save_signal(signal)
        if signal.action == SignalAction.HOLD:
            return

        cash_balance, equity = self._balances()
        if signal.action == SignalAction.BUY:
            decision = risk.can_open_position(signal, cash_balance, equity, position)
            if not decision.allowed:
                self.notifier.send(_entry_blocked_message(symbol, decision.reason))
                return
            size = risk.order_size_for_entry(signal.price, cash_balance, equity)
            order = OrderRequest(
                symbol=symbol,
                side=Side.BUY,
                size=size,
                order_type="market",
                price=None,
                client_order_id=OkxRestClient.client_order_id("B", symbol),
                reason=signal.reason,
            )
            self._execute_order(order, position, signal.price, risk)
        elif signal.action == SignalAction.SELL and position.is_open:
            order = OrderRequest(
                symbol=symbol,
                side=Side.SELL,
                size=position.base_qty,
                order_type="market",
                price=None,
                client_order_id=OkxRestClient.client_order_id("S", symbol),
                reason=signal.reason,
            )
            self._execute_order(order, position, signal.price, risk)

    def _execute_order(
        self,
        order: OrderRequest,
        position,
        fill_price_estimate: float,
        risk: RiskManager,
    ) -> None:
        if not self.settings.trading_enabled:
            result = OrderResult(
                ok=True,
                symbol=order.symbol,
                side=order.side,
                order_id="dry-run",
                client_order_id=order.client_order_id,
                raw={"dry_run": True},
            )
        else:
            result = self.exchange.place_order(order)
        self.storage.save_order(order, result)
        if not result.ok:
            risk.pause_symbol(order.symbol, f"order_failed:{result.error}")
            self.notifier.send(_order_failed_message(order.symbol, result.error))
            return

        if order.side == Side.BUY:
            self.storage.save_position(
                type(position)(
                    symbol=order.symbol,
                    base_qty=order.size,
                    avg_entry_price=fill_price_estimate,
                    highest_price=fill_price_estimate,
                )
            )
        else:
            pnl = position.base_qty * (fill_price_estimate - position.avg_entry_price)
            risk.record_trade_pnl(pnl)
            self.storage.save_position(type(position)(symbol=order.symbol))
        self.notifier.send(_order_recorded_message(order.symbol, order.side, order.reason))

    def _balances(self) -> tuple[float, float]:
        if not self.settings.trading_enabled:
            return 10_000.0, 10_000.0
        payload = self.exchange.get_balance("USDT")
        details = payload.get("data", [{}])[0].get("details", [])
        usdt = next((item for item in details if item.get("ccy") == "USDT"), {})
        cash = float(usdt.get("availBal") or usdt.get("cashBal") or 0)
        equity = float(payload.get("data", [{}])[0].get("totalEq") or cash)
        return cash, equity

    def _strategy(self) -> TrendPullbackStrategy:
        return TrendPullbackStrategy(
            ema_fast=self.settings.ema_fast,
            ema_slow=self.settings.ema_slow,
            rsi_period=self.settings.rsi_period,
            rsi_low=self.settings.rsi_low,
            stop_loss_pct=self.settings.stop_loss_pct,
            take_profit_pct=self.settings.take_profit_pct,
            trailing_stop_pct=self.settings.trailing_stop_pct,
        )


def _start_message(okx_demo: bool) -> str:
    mode = "模拟盘" if okx_demo else "实盘"
    return f"OKX量化机器人已启动（{mode}模式）。"


def _runtime_error_message(symbol: str, exc: Exception) -> str:
    return f"{symbol} 因运行时错误已暂停：{exc}"


def _entry_blocked_message(symbol: str, reason: str) -> str:
    return f"{symbol} 开仓被风控拦截：{_translate_reason(reason)}"


def _order_failed_message(symbol: str, error: str | None) -> str:
    detail = error or "未知错误"
    return f"{symbol} 下单失败，交易对已暂停：{detail}"


def _order_recorded_message(symbol: str, side: Side, reason: str) -> str:
    return f"{symbol} {_translate_side(side)}已记录：{_translate_reason(reason)}"


def _translate_side(side: Side) -> str:
    if side == Side.BUY:
        return "买入"
    if side == Side.SELL:
        return "卖出"
    return side.value


def _translate_reason(reason: str) -> str:
    reason_map = {
        "not_an_entry": "不是开仓信号",
        "max_consecutive_losses_reached": "连续亏损次数达到上限",
        "max_daily_loss_reached": "当日亏损达到上限",
        "symbol_position_limit_reached": "该交易对仓位已达到上限",
        "no_cash_available": "可用现金不足",
        "risk_ok": "风控通过",
        "not_enough_data": "K线数据不足",
        "indicators_not_ready": "指标尚未准备好",
        "trend_ok_reclaim_ema_fast_rsi_rebound": "趋势向上，价格收复快线，RSI反弹",
        "no_entry": "暂无开仓信号",
        "stop_loss": "触发止损",
        "take_profit": "触发止盈",
        "trailing_stop": "触发移动止损",
        "trend_lost": "趋势转弱",
        "failed_reclaim": "收复失败",
        "hold_position": "继续持仓",
    }
    if reason.endswith(" is paused"):
        symbol = reason.removesuffix(" is paused")
        return f"{symbol} 已暂停"
    return reason_map.get(reason, reason)
