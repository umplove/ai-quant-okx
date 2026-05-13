from __future__ import annotations

from dataclasses import dataclass

from okx_quant_bot.models import Candle, Position, Signal, SignalAction
from okx_quant_bot.strategy.indicators import ema, rsi


@dataclass(frozen=True)
class TrendPullbackStrategy:
    ema_fast: int = 20
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_low: float = 35.0
    stop_loss_pct: float = 0.015
    take_profit_pct: float = 0.03
    trailing_stop_pct: float = 0.01

    def generate(self, candles: list[Candle], position: Position) -> Signal:
        if not candles:
            raise ValueError("candles cannot be empty")
        if position.is_open:
            latest = candles[-1]
            fast_now: float | None = None
            slow_now: float | None = None
            if len(candles) >= self.ema_fast:
                fast_now = ema([c.close for c in candles], self.ema_fast)[-1]
            if len(candles) >= self.ema_slow:
                slow_now = ema([c.close for c in candles], self.ema_slow)[-1]
            return self._exit_signal(latest, position, fast_now, slow_now)

        if len(candles) < max(self.ema_slow, self.rsi_period + 2) + 2:
            latest = candles[-1]
            return Signal(latest.symbol, latest.ts, SignalAction.HOLD, latest.close, "not_enough_data")

        closes = [c.close for c in candles]
        fast = ema(closes, self.ema_fast)
        slow = ema(closes, self.ema_slow)
        rsi_values = rsi(closes, self.rsi_period)
        idx = len(candles) - 1
        prev = idx - 1
        latest = candles[idx]

        if None in {fast[idx], fast[prev], slow[idx], rsi_values[idx], rsi_values[prev]}:
            return Signal(latest.symbol, latest.ts, SignalAction.HOLD, latest.close, "indicators_not_ready")

        trend_ok = latest.close > slow[idx]
        reclaimed_fast = closes[prev] <= fast[prev] and latest.close > fast[idx]
        rsi_rebound = rsi_values[prev] <= self.rsi_low and rsi_values[idx] > rsi_values[prev]
        if trend_ok and reclaimed_fast and rsi_rebound:
            return Signal(
                latest.symbol,
                latest.ts,
                SignalAction.BUY,
                latest.close,
                "trend_ok_reclaim_ema_fast_rsi_rebound",
            )
        return Signal(latest.symbol, latest.ts, SignalAction.HOLD, latest.close, "no_entry")

    def _exit_signal(
        self,
        latest: Candle,
        position: Position,
        fast_now: float | None,
        slow_now: float | None,
    ) -> Signal:
        entry = position.avg_entry_price
        highest = max(position.highest_price, latest.close, entry)
        if latest.close <= entry * (1 - self.stop_loss_pct):
            return Signal(latest.symbol, latest.ts, SignalAction.SELL, latest.close, "stop_loss")
        if latest.close >= entry * (1 + self.take_profit_pct):
            return Signal(latest.symbol, latest.ts, SignalAction.SELL, latest.close, "take_profit")
        if latest.close <= highest * (1 - self.trailing_stop_pct):
            return Signal(latest.symbol, latest.ts, SignalAction.SELL, latest.close, "trailing_stop")
        if slow_now is not None and latest.close < slow_now:
            return Signal(latest.symbol, latest.ts, SignalAction.SELL, latest.close, "trend_lost")
        if fast_now is not None and latest.close < fast_now and latest.close < entry:
            return Signal(latest.symbol, latest.ts, SignalAction.SELL, latest.close, "failed_reclaim")
        return Signal(latest.symbol, latest.ts, SignalAction.HOLD, latest.close, "hold_position")
