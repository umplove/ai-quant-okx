from __future__ import annotations

import sqlite3
import time
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
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma busy_timeout = 30000")
        conn.execute("pragma journal_mode = WAL")
        conn.execute("pragma synchronous = NORMAL")
        return conn

    @contextmanager
    def session(self):
        conn = self.connect()
        try:
            yield conn
            for attempt in range(3):
                try:
                    conn.commit()
                    break
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    if "locked" not in str(exc).lower() or attempt >= 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))
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

                create table if not exists ai_call_audits (
                    id integer primary key autoincrement,
                    symbol text not null,
                    intent text not null,
                    ok integer not null,
                    action text not null,
                    confidence real not null,
                    prompt_chars integer not null,
                    response_chars integer not null,
                    duration_ms integer not null,
                    error text not null,
                    reason text not null,
                    prompt_tokens integer not null default 0,
                    completion_tokens integer not null default 0,
                    total_tokens integer not null default 0,
                    attempted_tokens integer not null default 0,
                    retry_count integer not null default 0,
                    created_at text default current_timestamp
                );

                create table if not exists ai_training_runs (
                    week_key text primary key,
                    target_tokens integer not null,
                    prompt_tokens integer not null default 0,
                    completion_tokens integer not null default 0,
                    total_tokens integer not null default 0,
                    attempted_tokens integer not null default 0,
                    task_count integer not null default 0,
                    success_count integer not null default 0,
                    error_count integer not null default 0,
                    updated_at text default current_timestamp
                );

                create table if not exists shadow_decisions (
                    id integer primary key autoincrement,
                    symbol text not null,
                    market_type text not null,
                    strategy text not null,
                    action text not null,
                    confidence real not null,
                    reason text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists execution_decisions (
                    id integer primary key autoincrement,
                    symbol text not null,
                    intent text not null,
                    action text not null,
                    entry_mode text not null,
                    exit_mode text not null,
                    size_mode text not null,
                    stop_mode text not null,
                    replace_mode text not null,
                    confidence real not null,
                    reason text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists trade_attributions (
                    id integer primary key autoincrement,
                    symbol text not null,
                    pnl_usdt real not null,
                    return_pct real not null,
                    category text not null,
                    confidence real not null,
                    reason text not null,
                    market_regime text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists market_regimes (
                    id integer primary key autoincrement,
                    regime text not null,
                    confidence real not null,
                    reason text not null,
                    raw text not null,
                    created_at text default current_timestamp
                );

                create table if not exists bot_errors (
                    id integer primary key autoincrement,
                    source text not null,
                    message text not null,
                    details text not null,
                    created_at text default current_timestamp
                );

                create table if not exists config_snapshots (
                    id integer primary key autoincrement,
                    snapshot text not null,
                    created_at text default current_timestamp
                );
                """
            )
            self._ensure_column(conn, "ai_call_audits", "prompt_tokens", "integer not null default 0")
            self._ensure_column(conn, "ai_call_audits", "completion_tokens", "integer not null default 0")
            self._ensure_column(conn, "ai_call_audits", "total_tokens", "integer not null default 0")
            self._ensure_column(conn, "ai_call_audits", "attempted_tokens", "integer not null default 0")
            self._ensure_column(conn, "ai_call_audits", "retry_count", "integer not null default 0")
            self._ensure_column(conn, "ai_training_runs", "attempted_tokens", "integer not null default 0")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

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

    def recent_strategy_lessons(self, limit: int = 20) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, pnl_usdt, return_pct, summary
                from strategy_lessons
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

    def save_ai_call_audit(
        self,
        symbol: str,
        intent: str,
        ok: bool,
        action: str = "hold",
        confidence: float = 0.0,
        prompt_chars: int = 0,
        response_chars: int = 0,
        duration_ms: int = 0,
        error: str = "",
        reason: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        attempted_tokens: int = 0,
        retry_count: int = 0,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into ai_call_audits(
                    symbol, intent, ok, action, confidence, prompt_chars, response_chars,
                    duration_ms, error, reason, prompt_tokens, completion_tokens,
                    total_tokens, attempted_tokens, retry_count
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    intent,
                    int(ok),
                    action,
                    confidence,
                    int(prompt_chars),
                    int(response_chars),
                    int(duration_ms),
                    error[:1000],
                    reason[:1000],
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    int(attempted_tokens or total_tokens or max(0, int(prompt_chars) // 2)),
                    int(retry_count),
                ),
            )

    def recent_ai_call_summary(self, limit: int = 200) -> str:
        with self.session() as conn:
            rows = conn.execute(
                """
                select intent, ok, action, prompt_chars, response_chars, duration_ms, error,
                       prompt_tokens, completion_tokens, total_tokens, attempted_tokens, retry_count
                from ai_call_audits
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        if not rows:
            return "AI调用: 暂无"
        total = len(rows)
        ok_count = sum(1 for row in rows if row["ok"])
        buy_count = sum(1 for row in rows if row["action"] == "buy")
        sell_count = sum(1 for row in rows if row["action"] == "sell")
        prompt_chars = sum(int(row["prompt_chars"]) for row in rows)
        response_chars = sum(int(row["response_chars"]) for row in rows)
        total_tokens = sum(int(row["total_tokens"]) for row in rows)
        attempted_tokens = sum(int(row["attempted_tokens"]) for row in rows)
        retry_count = sum(int(row["retry_count"]) for row in rows)
        avg_ms = sum(int(row["duration_ms"]) for row in rows) / total
        errors = [str(row["error"]) for row in rows if row["error"]]
        error_tail = f"; 最近错误: {errors[0][:60]}" if errors else ""
        return (
            f"AI调用: {ok_count}/{total}成功; buy={buy_count}; sell={sell_count}; "
            f"输入约{prompt_chars}字; 输出约{response_chars}字; 成功token={total_tokens}; "
            f"估算尝试token={attempted_tokens}; "
            f"重试={retry_count}; 平均{avg_ms:.0f}ms"
            f"{error_tail}"
        )

    def add_training_usage(
        self,
        week_key: str,
        target_tokens: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        attempted_tokens: int | bool = 0,
        ok: bool | None = None,
    ) -> None:
        if ok is None:
            ok = bool(attempted_tokens)
            attempted_tokens = total_tokens
        with self.session() as conn:
            conn.execute(
                """
                insert into ai_training_runs(
                    week_key, target_tokens, prompt_tokens, completion_tokens,
                    total_tokens, attempted_tokens, task_count, success_count, error_count
                ) values (?, ?, ?, ?, ?, ?, 1, ?, ?)
                on conflict(week_key) do update set
                    target_tokens = excluded.target_tokens,
                    prompt_tokens = ai_training_runs.prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = ai_training_runs.completion_tokens + excluded.completion_tokens,
                    total_tokens = ai_training_runs.total_tokens + excluded.total_tokens,
                    attempted_tokens = ai_training_runs.attempted_tokens + excluded.attempted_tokens,
                    task_count = ai_training_runs.task_count + 1,
                    success_count = ai_training_runs.success_count + excluded.success_count,
                    error_count = ai_training_runs.error_count + excluded.error_count,
                    updated_at = current_timestamp
                """,
                (
                    week_key,
                    int(target_tokens),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    int(attempted_tokens or total_tokens or 0),
                    int(ok),
                    0 if ok else 1,
                ),
            )

    def training_summary(self, week_key: str, target_tokens: int) -> str:
        with self.session() as conn:
            row = conn.execute("select * from ai_training_runs where week_key = ?", (week_key,)).fetchone()
        if row is None:
            return f"训练进度: 成功0/{target_tokens} token，估算尝试0，完成率0.00%，任务0，成功0，失败0"
        total_tokens = int(row["total_tokens"])
        attempted_tokens = int(row["attempted_tokens"])
        pct = 0.0 if target_tokens <= 0 else total_tokens / target_tokens * 100.0
        return (
            f"训练进度: 成功{total_tokens}/{target_tokens} token，估算尝试{attempted_tokens}，完成率{pct:.2f}%，"
            f"任务{row['task_count']}，成功{row['success_count']}，失败{row['error_count']}"
        )

    def save_shadow_decision(
        self,
        symbol: str,
        market_type: str,
        strategy: str,
        action: str,
        confidence: float,
        reason: str,
        raw: str = "",
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into shadow_decisions(symbol, market_type, strategy, action, confidence, reason, raw)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, market_type, strategy, action, float(confidence), reason[:1000], raw[:4000]),
            )

    def recent_shadow_decisions(self, limit: int = 8) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, market_type, strategy, action, confidence, reason
                from shadow_decisions
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{r['symbol']} {r['market_type']}/{r['strategy']} -> {r['action']} conf={r['confidence']:.2f}: {r['reason']}"
            for r in rows
        ]

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

    def save_execution_decision(
        self,
        symbol: str,
        intent: str,
        action: str,
        entry_mode: str,
        exit_mode: str,
        size_mode: str,
        stop_mode: str,
        replace_mode: str,
        confidence: float,
        reason: str,
        raw: str = "",
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into execution_decisions(
                    symbol, intent, action, entry_mode, exit_mode, size_mode, stop_mode,
                    replace_mode, confidence, reason, raw
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    intent,
                    action,
                    entry_mode,
                    exit_mode,
                    size_mode,
                    stop_mode,
                    replace_mode,
                    float(confidence),
                    reason[:1000],
                    raw[:4000],
                ),
            )

    def latest_execution_decision(self, symbol: str, intent: str, max_age_seconds: int = 180) -> dict | None:
        with self.session() as conn:
            row = conn.execute(
                """
                select *
                from execution_decisions
                where symbol = ? and intent = ? and created_at >= datetime('now', ?)
                order by created_at desc, id desc
                limit 1
                """,
                (symbol, intent, f"-{int(max_age_seconds)} seconds"),
            ).fetchone()
        return None if row is None else dict(row)

    def recent_execution_decisions(self, limit: int = 10) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, intent, action, entry_mode, exit_mode, size_mode, replace_mode, confidence, reason
                from execution_decisions
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{r['symbol']} {r['intent']} -> {r['action']} entry={r['entry_mode']} exit={r['exit_mode']} "
            f"size={r['size_mode']} replace={r['replace_mode']} conf={r['confidence']:.2f}: {r['reason']}"
            for r in rows
        ]

    def save_trade_attribution(
        self,
        symbol: str,
        pnl_usdt: float,
        return_pct: float,
        category: str,
        confidence: float,
        reason: str,
        market_regime: str = "",
        raw: str = "",
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into trade_attributions(
                    symbol, pnl_usdt, return_pct, category, confidence, reason, market_regime, raw
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    float(pnl_usdt),
                    float(return_pct),
                    category,
                    float(confidence),
                    reason[:1000],
                    market_regime[:100],
                    raw[:4000],
                ),
            )

    def recent_trade_attributions(self, limit: int = 10) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, pnl_usdt, return_pct, category, confidence, reason, market_regime
                from trade_attributions
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{r['symbol']} {r['category']} pnl={r['pnl_usdt']:+.2f} return={r['return_pct']:+.2f}% "
            f"regime={r['market_regime'] or '未知'} conf={r['confidence']:.2f}: {r['reason']}"
            for r in rows
        ]

    def save_market_regime(self, regime: str, confidence: float, reason: str, raw: str = "") -> None:
        with self.session() as conn:
            conn.execute(
                "insert into market_regimes(regime, confidence, reason, raw) values (?, ?, ?, ?)",
                (regime[:100], float(confidence), reason[:1000], raw[:4000]),
            )

    def latest_market_regime(self) -> dict | None:
        with self.session() as conn:
            row = conn.execute(
                """
                select regime, confidence, reason, raw, created_at
                from market_regimes
                order by created_at desc, id desc
                limit 1
                """
            ).fetchone()
        return None if row is None else dict(row)

    def recent_market_regimes(self, limit: int = 5) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select regime, confidence, reason, created_at
                from market_regimes
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [f"{r['created_at']} {r['regime']} conf={r['confidence']:.2f}: {r['reason']}" for r in rows]

    def latest_ai_decision(self, symbol: str, intent: str, max_age_seconds: int = 180) -> dict | None:
        with self.session() as conn:
            row = conn.execute(
                """
                select symbol, intent, action, confidence, reason, raw, created_at
                from ai_decisions
                where symbol = ? and intent = ? and created_at >= datetime('now', ?)
                order by created_at desc, id desc
                limit 1
                """,
                (symbol, intent, f"-{int(max_age_seconds)} seconds"),
            ).fetchone()
        return None if row is None else dict(row)

    def save_bot_error(self, source: str, message: str, details: str = "") -> None:
        with self.session() as conn:
            conn.execute(
                "insert into bot_errors(source, message, details) values (?, ?, ?)",
                (source[:100], message[:1000], details[:4000]),
            )

    def recent_bot_errors(self, limit: int = 10) -> list[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select source, message, details, created_at
                from bot_errors
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            f"{r['created_at']} [{r['source']}] {r['message']}"
            + (f" | {r['details'][:120]}" if r["details"] else "")
            for r in rows
        ]

    def save_config_snapshot(self, snapshot: str) -> None:
        with self.session() as conn:
            conn.execute("insert into config_snapshots(snapshot) values (?)", (snapshot[:4000],))

    def symbol_experience_biases(self, limit: int = 200) -> dict[str, float]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select symbol, pnl_usdt, return_pct
                from strategy_lessons
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        biases: dict[str, float] = {}
        for idx, row in enumerate(rows):
            decay = 0.96 ** idx
            return_pct = max(-5.0, min(float(row["return_pct"]), 5.0))
            pnl_score = max(-2.0, min(float(row["pnl_usdt"]) / 20.0, 2.0))
            biases[row["symbol"]] = biases.get(row["symbol"], 0.0) + (return_pct + pnl_score) * decay
        return {symbol: max(-8.0, min(score, 8.0)) for symbol, score in biases.items()}
