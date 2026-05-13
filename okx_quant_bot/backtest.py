from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from okx_quant_bot.models import BacktestResult, Candle, Position, SignalAction
from okx_quant_bot.strategy import TrendPullbackStrategy


def load_candles_csv(path: Path | str) -> dict[str, list[Candle]]:
    grouped: dict[str, list[Candle]] = defaultdict(list)
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            candle = Candle(
                symbol=row["symbol"].strip().upper(),
                ts=int(row["ts"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
            )
            grouped[candle.symbol].append(candle)
    return {symbol: sorted(rows, key=lambda c: c.ts) for symbol, rows in grouped.items()}


def run_backtest(
    symbol: str,
    candles: list[Candle],
    strategy: TrendPullbackStrategy,
    starting_cash: float = 10_000.0,
    trade_fraction: float = 0.10,
) -> BacktestResult:
    cash = starting_cash
    position = Position(symbol=symbol)
    equity_curve: list[float] = []
    pnls: list[float] = []

    for idx in range(len(candles)):
        window = candles[: idx + 1]
        latest = window[-1]
        if position.is_open:
            position.highest_price = max(position.highest_price, latest.close)
        equity = cash + position.market_value(latest.close)
        equity_curve.append(equity)
        signal = strategy.generate(window, position)

        if signal.action == SignalAction.BUY and not position.is_open:
            budget = min(cash, equity * trade_fraction)
            if budget <= 0:
                continue
            qty = budget / latest.close
            cash -= budget
            position = Position(symbol=symbol, base_qty=qty, avg_entry_price=latest.close, highest_price=latest.close)
        elif signal.action == SignalAction.SELL and position.is_open:
            proceeds = position.base_qty * latest.close
            cost = position.base_qty * position.avg_entry_price
            cash += proceeds
            pnls.append(proceeds - cost)
            position = Position(symbol=symbol)

    final_price = candles[-1].close if candles else 0.0
    ending_cash = cash + position.market_value(final_price)
    max_drawdown = _max_drawdown(equity_curve)
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = [abs(pnl) for pnl in pnls if pnl < 0]
    gains = [pnl for pnl in pnls if pnl > 0]
    profit_factor = sum(gains) / sum(losses) if losses else (float("inf") if gains else 0.0)
    return BacktestResult(
        symbol=symbol,
        starting_cash=starting_cash,
        ending_cash=ending_cash,
        total_return_pct=((ending_cash - starting_cash) / starting_cash * 100) if starting_cash else 0.0,
        max_drawdown_pct=max_drawdown * 100,
        win_rate_pct=(wins / len(pnls) * 100) if pnls else 0.0,
        profit_factor=profit_factor,
        trade_count=len(pnls),
    )


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd

