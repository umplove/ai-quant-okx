from __future__ import annotations

from okx_quant_bot.models import BacktestResult


def format_backtest_result(result: BacktestResult) -> str:
    return "\n".join(
        [
            f"Symbol: {result.symbol}",
            f"Starting cash: {result.starting_cash:.2f}",
            f"Ending cash: {result.ending_cash:.2f}",
            f"Total return: {result.total_return_pct:.2f}%",
            f"Max drawdown: {result.max_drawdown_pct:.2f}%",
            f"Win rate: {result.win_rate_pct:.2f}%",
            f"Profit factor: {result.profit_factor:.2f}",
            f"Trades: {result.trade_count}",
        ]
    )

