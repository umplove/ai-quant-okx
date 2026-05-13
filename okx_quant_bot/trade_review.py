from __future__ import annotations

from okx_quant_bot.models import MarketTicker, Position, TradeReview


class TradeReviewEngine:
    def mark_to_market(
        self,
        positions: list[Position],
        tickers: list[MarketTicker],
        note: str = "",
    ) -> list[TradeReview]:
        prices = {ticker.symbol: ticker.last for ticker in tickers}
        reviews: list[TradeReview] = []
        for position in positions:
            if not position.is_open:
                continue
            current_price = prices.get(position.symbol)
            if current_price is None or current_price <= 0:
                continue
            reviews.append(_review_position(position, current_price, note))
        return reviews


def _review_position(position: Position, current_price: float, note: str) -> TradeReview:
    pnl = position.base_qty * (current_price - position.avg_entry_price)
    cost = position.base_qty * position.avg_entry_price
    return_pct = 0.0 if cost <= 0 else pnl / cost * 100.0
    status = "赚钱" if pnl > 0 else "亏钱" if pnl < 0 else "持平"
    summary = (
        f"{status}; entry={position.avg_entry_price:.8g}; "
        f"mark={current_price:.8g}; pnl={pnl:+.2f}USDT; return={return_pct:+.2f}%"
    )
    if note:
        summary += f"; {note}"
    return TradeReview(
        symbol=position.symbol,
        phase="mark_to_market",
        entry_price=position.avg_entry_price,
        current_price=current_price,
        size=position.base_qty,
        pnl_usdt=pnl,
        return_pct=return_pct,
        summary=summary,
        raw=summary,
    )
