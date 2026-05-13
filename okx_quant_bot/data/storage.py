from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from okx_quant_bot.models import (
    Candle,
    CandidateScore,
    InfoSignal,
    IntelligenceItem,
    MarketTicker,
    OrderRequest,
    OrderResult,
    Position,
    Signal,
    StopLossOrder,
    TradeReview,
)


class Storage:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def session(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.session() as conn:
            conn.executescript(
                """
                create table if not exists candles (
                    symbol text not null,
                    ts integer not null,
                    open real not null,
                    high real not null,
                    low real not null,
                    close real not null,
                    volume real not null,
                    primary key (symbol, ts)
                );

                create table if not exists signals (
                    id integer primary key autoincrement,
                    symbol text not null,
                    ts integer not null,
                    action text not null,
                    price real not null,
                    reason text not null,
                    created_at text default current_timestamp
                );

                create table if not exists orders (
                    id integer primary key autoincrement,
                    symbol text not null,
                    side text not null,
                    size real not null,
                    order_type text not null,
                    price real,
                    client_order_id text not null unique,
                    exchange_order_id text,
                    ok integer not null,
                    reason text not null,
                    error text,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists positions (
                    symbol text primary key,
                    base_qty real not null,
                    avg_entry_price real not null,
                    highest_price real not null,
                    updated_at text default current_timestamp
                );

                create table if not exists bot_state (
                    key text primary key,
                    value text not null,
                    updated_at text default current_timestamp
                );

                create table if not exists market_snapshots (
                    id integer primary key autoincrement,
                    symbol text not null,
                    ts integer not null,
                    last real not null,
                    open_24h real not null,
                    high_24h real not null,
                    low_24h real not null,
                    volume_quote_24h real not null,
                    change_pct_24h real not null,
                    amplitude_pct_24h real not null,
                    created_at text default current_timestamp
                );

                create table if not exists info_signals (
                    id integer primary key autoincrement,
                    source text not null,
                    symbol text not null,
                    score real not null,
                    title text not null,
                    url text not null,
                    created_at text default current_timestamp
                );

                create table if not exists candidate_scores (
                    id integer primary key autoincrement,
                    symbol text not null,
                    price real not null,
                    change_pct_24h real not null,
                    amplitude_pct_24h real not null,
                    volume_quote_24h real not null,
                    news_score real not null,
                    polymarket_score real not null,
                    total_score real not null,
                    confirmed integer not null,
                    reason text not null,
                    created_at text default current_timestamp
                );

                create table if not exists stop_loss_orders (
                    id integer primary key autoincrement,
                    symbol text not null,
                    algo_id text,
                    client_order_id text not null unique,
                    stop_price real not null,
                    size real not null,
                    ok integer not null,
                    raw text not null,
                    error text,
                    created_at text default current_timestamp
                );

                create table if not exists daily_reports (
                    report_date text primary key,
                    summary text not null,
                    created_at text default current_timestamp
                );

                create table if not exists strategy_lessons (
                    id integer primary key autoincrement,
                    symbol text not null,
                    pnl_usdt real not null,
                    return_pct real not null,
                    active integer not null,
                    summary text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists intelligence_items (
                    id integer primary key autoincrement,
                    source text not null,
                    symbol text not null,
                    title text not null,
                    url text not null,
                    score real not null,
                    raw text not null,
                    created_at text default current_timestamp,
                    unique(source, symbol, title, url)
                );

                create table if not exists trade_reviews (
                    id integer primary key autoincrement,
                    symbol text not null,
                    phase text not null,
                    entry_price real not null,
                    current_price real not null,
                    size real not null,
                    pnl_usdt real not null,
                    return_pct real not null,
                    summary text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists ai_decisions (
                    id integer primary key autoincrement,
                    symbol text not null,
                    intent text not null,
                    action text not null,
                    confidence real not null,
                    reason text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );
                """
            )

    def save_candles(self, candles: Iterable[Candle]) -> None:
        with self.session() as conn:
            conn.executemany(
                """
                insert or replace into candles(symbol, ts, open, high, low, close, volume)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (c.symbol, c.ts, c.open, c.high, c.low, c.close, c.volume)
                    for c in candles
                ],
            )

    def load_candles(self, symbol: str, limit: int = 500) -> list[Candle]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select * from candles where symbol = ?
                order by ts desc limit ?
                """,
                (symbol, limit),
            ).fetchall()
        return [
            Candle(r["symbol"], r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in reversed(rows)
        ]

    def save_signal(self, signal: Signal) -> None:
        with self.session() as conn:
            conn.execute(
                "insert into signals(symbol, ts, action, price, reason) values (?, ?, ?, ?, ?)",
                (signal.symbol, signal.ts, signal.action.value, signal.price, signal.reason),
            )

    def save_order(self, request: OrderRequest, result: OrderResult) -> None:
        import json

        with self.session() as conn:
            conn.execute(
                """
                insert or replace into orders(
                    symbol, side, size, order_type, price, client_order_id, exchange_order_id,
                    ok, reason, error, raw
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.symbol,
                    request.side.value,
                    request.size,
                    request.order_type,
                    request.price,
                    request.client_order_id,
                    result.order_id,
                    int(result.ok),
                    request.reason,
                    result.error,
                    json.dumps(result.raw, ensure_ascii=False),
                ),
            )

    def save_market_snapshots(self, tickers: Iterable[MarketTicker]) -> None:
        with self.session() as conn:
            conn.executemany(
                """
                insert into market_snapshots(
                    symbol, ts, last, open_24h, high_24h, low_24h, volume_quote_24h,
                    change_pct_24h, amplitude_pct_24h
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.symbol,
                        t.ts,
                        t.last,
                        t.open_24h,
                        t.high_24h,
                        t.low_24h,
                        t.volume_quote_24h,
                        t.change_pct_24h,
                        t.amplitude_pct_24h,
                    )
                    for t in tickers
                ],
            )

    def save_info_signals(self, signals: Iterable[InfoSignal]) -> None:
        with self.session() as conn:
            conn.executemany(
                """
                insert into info_signals(source, symbol, score, title, url)
                values (?, ?, ?, ?, ?)
                """,
                [(s.source, s.symbol, s.score, s.title, s.url) for s in signals],
            )

    def save_candidate_scores(self, candidates: Iterable[CandidateScore]) -> None:
        with self.session() as conn:
            conn.executemany(
                """
                insert into candidate_scores(
                    symbol, price, change_pct_24h, amplitude_pct_24h, volume_quote_24h,
                    news_score, polymarket_score, total_score, confirmed, reason
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        c.symbol,
                        c.price,
                        c.change_pct_24h,
                        c.amplitude_pct_24h,
                        c.volume_quote_24h,
                        c.news_score,
                        c.polymarket_score,
                        c.total_score,
                        int(c.confirmed),
                        c.reason,
                    )
                    for c in candidates
                ],
            )

    def save_stop_loss_order(self, order: StopLossOrder) -> None:
        import json

        with self.session() as conn:
            conn.execute(
                """
                insert or replace into stop_loss_orders(
                    symbol, algo_id, client_order_id, stop_price, size, ok, raw, error
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.symbol,
                    order.algo_id,
                    order.client_order_id,
                    order.stop_price,
                    order.size,
                    int(order.ok),
                    json.dumps(order.raw, ensure_ascii=False),
                    order.error,
                ),
            )

    def save_daily_report(self, report_date: str, summary: str) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into daily_reports(report_date, summary) values (?, ?)
                on conflict(report_date) do update set
                    summary = excluded.summary,
                    created_at = current_timestamp
                """,
                (report_date, summary),
            )

    def get_position(self, symbol: str) -> Position:
        with self.session() as conn:
            row = conn.execute("select * from positions where symbol = ?", (symbol,)).fetchone()
        if row is None:
            return Position(symbol=symbol)
        return Position(row["symbol"], row["base_qty"], row["avg_entry_price"], row["highest_price"])

    def open_position_count(self) -> int:
        with self.session() as conn:
            row = conn.execute(
                """
                select count(*) as count from positions
                where base_qty > 0 and avg_entry_price > 0
                """
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def open_positions(self) -> list[Position]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select * from positions
                where base_qty > 0 and avg_entry_price > 0
                order by updated_at desc
                """
            ).fetchall()
        return [
            Position(row["symbol"], row["base_qty"], row["avg_entry_price"], row["highest_price"])
            for row in rows
        ]

    def save_position(self, position: Position) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into positions(symbol, base_qty, avg_entry_price, highest_price)
                values (?, ?, ?, ?)
                on conflict(symbol) do update set
                    base_qty = excluded.base_qty,
                    avg_entry_price = excluded.avg_entry_price,
                    highest_price = excluded.highest_price,
                    updated_at = current_timestamp
                """,
                (
                    position.symbol,
                    position.base_qty,
                    position.avg_entry_price,
                    position.highest_price,
                ),
            )

    def set_state(self, key: str, value: str) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into bot_state(key, value) values (?, ?)
                on conflict(key) do update set value = excluded.value, updated_at = current_timestamp
                """,
                (key, value),
            )

    def get_state(self, key: str, default: str = "") -> str:
        with self.session() as conn:
            row = conn.execute("select value from bot_state where key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def save_strategy_lesson(
        self,
        symbol: str,
        pnl_usdt: float,
        return_pct: float,
        summary: str,
        raw: str = "",
    ) -> None:
        active = int(pnl_usdt > 0)
        with self.session() as conn:
            conn.execute(
                """
                insert into strategy_lessons(symbol, pnl_usdt, return_pct, active, summary, raw)
                values (?, ?, ?, ?, ?, ?)
                """,
                (symbol, pnl_usdt, return_pct, active, summary[:1000], raw[:4000]),
            )

    def active_strategy_lessons(self, limit: int = 8) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, pnl_usdt, return_pct, summary
                from strategy_lessons
                where active = 1
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{row['symbol']} pnl={row['pnl_usdt']:+.2f}USDT return={row['return_pct']:+.2f}%: {row['summary']}"
            for row in rows
        ]

    def save_intelligence_items(self, items: Iterable[IntelligenceItem]) -> None:
        with self.session() as conn:
            conn.executemany(
                """
                insert or ignore into intelligence_items(source, symbol, title, url, score, raw)
                values (?, ?, ?, ?, ?, ?)
                """,
                [(i.source, i.symbol, i.title, i.url, i.score, i.raw) for i in items],
            )

    def recent_intelligence(self, limit: int = 20) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select source, symbol, title, score
                from intelligence_items
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [f"{r['source']} {r['symbol']} score={r['score']:.1f}: {r['title']}" for r in rows]

    def save_trade_review(self, review: TradeReview) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into trade_reviews(
                    symbol, phase, entry_price, current_price, size, pnl_usdt,
                    return_pct, summary, raw
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.symbol,
                    review.phase,
                    review.entry_price,
                    review.current_price,
                    review.size,
                    review.pnl_usdt,
                    review.return_pct,
                    review.summary[:1000],
                    review.raw[:4000],
                ),
            )

    def recent_trade_reviews(self, limit: int = 8) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, phase, pnl_usdt, return_pct, summary
                from trade_reviews
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{r['symbol']} {r['phase']} pnl={r['pnl_usdt']:+.2f}USDT "
            f"return={r['return_pct']:+.2f}%: {r['summary']}"
            for r in rows
        ]

    def save_ai_decision(
        self,
        symbol: str,
        intent: str,
        action: str,
        confidence: float,
        reason: str,
        raw: str = "",
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into ai_decisions(symbol, intent, action, confidence, reason, raw)
                values (?, ?, ?, ?, ?, ?)
                """,
                (symbol, intent, action, confidence, reason[:1000], raw[:4000]),
            )

    def recent_ai_decisions(self, limit: int = 12) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, intent, action, confidence, reason
                from ai_decisions
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{r['symbol']} {r['intent']} -> {r['action']} conf={r['confidence']:.2f}: {r['reason']}"
            for r in rows
        ]
