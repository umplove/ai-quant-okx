from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Candle:
    symbol: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts / 1000, tz=timezone.utc)

    @classmethod
    def from_okx(cls, symbol: str, row: list[str]) -> "Candle":
        return cls(
            symbol=symbol,
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5] or 0),
        )


@dataclass(frozen=True)
class Signal:
    symbol: str
    ts: int
    action: SignalAction
    price: float
    reason: str


@dataclass
class Position:
    symbol: str
    base_qty: float = 0.0
    avg_entry_price: float = 0.0
    highest_price: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.base_qty > 0 and self.avg_entry_price > 0

    def market_value(self, price: float) -> float:
        return self.base_qty * price


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    size: float
    order_type: str
    price: float | None
    client_order_id: str
    reason: str
    target_currency: str | None = None
    stop_loss_price: float | None = None


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    symbol: str
    side: Side
    order_id: str | None
    client_order_id: str
    raw: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    starting_cash: float
    ending_cash: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    trade_count: int


@dataclass(frozen=True)
class MarketTicker:
    symbol: str
    last: float
    open_24h: float
    high_24h: float
    low_24h: float
    volume_quote_24h: float
    ts: int

    @property
    def change_pct_24h(self) -> float:
        return 0.0 if self.open_24h <= 0 else (self.last - self.open_24h) / self.open_24h

    @property
    def amplitude_pct_24h(self) -> float:
        return 0.0 if self.open_24h <= 0 else (self.high_24h - self.low_24h) / self.open_24h

    @classmethod
    def from_okx(cls, row: dict[str, Any]) -> "MarketTicker":
        open_24h = float(row.get("open24h") or row.get("sodUtc0") or row.get("last") or 0)
        return cls(
            symbol=str(row.get("instId", "")),
            last=float(row.get("last") or 0),
            open_24h=open_24h,
            high_24h=float(row.get("high24h") or 0),
            low_24h=float(row.get("low24h") or 0),
            volume_quote_24h=float(row.get("volCcy24h") or 0),
            ts=int(row.get("ts") or 0),
        )


@dataclass(frozen=True)
class InfoSignal:
    source: str
    symbol: str
    score: float
    title: str
    url: str = ""


@dataclass(frozen=True)
class CandidateScore:
    symbol: str
    price: float
    change_pct_24h: float
    amplitude_pct_24h: float
    volume_quote_24h: float
    news_score: float
    polymarket_score: float
    total_score: float
    reason: str
    confirmed: bool


@dataclass(frozen=True)
class StopLossPlan:
    symbol: str
    entry_price: float
    size: float
    stop_price: float
    risk_usdt: float
    mode: str


@dataclass(frozen=True)
class StopLossOrder:
    symbol: str
    algo_id: str | None
    client_order_id: str
    stop_price: float
    size: float
    ok: bool
    raw: dict[str, Any]
    error: str | None = None
