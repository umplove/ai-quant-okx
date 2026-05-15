from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from okx_quant_bot.backtest import load_candles_csv, run_backtest
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.doctor import Doctor, format_results
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.notify import Notifier
from okx_quant_bot.report import format_backtest_result
from okx_quant_bot.runner import BotRunner
from okx_quant_bot.momentum_runner import MomentumBotRunner
from okx_quant_bot.strategy import TrendPullbackStrategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="okx-quant-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Only validate local configuration; skip OKX and Telegram calls.",
    )

    fetch = sub.add_parser("fetch-candles")
    fetch.add_argument("--limit", type=int, default=300)

    backtest = sub.add_parser("backtest")
    backtest.add_argument("--csv", required=True, type=Path)
    backtest.add_argument("--cash", type=float, default=10_000.0)

    sub.add_parser("run")
    sub.add_parser("scan-momentum")
    sub.add_parser("run-momentum")
    sub.add_parser("telegram-diagnose")
    delete_webhook = sub.add_parser("telegram-delete-webhook")
    delete_webhook.add_argument("--drop-pending-updates", action="store_true")
    live_db = sub.add_parser("prepare-live-db")
    live_db.add_argument("--source", type=Path, default=None)
    live_db.add_argument("--dest", type=Path, default=Path("data/live.sqlite3"))
    live_db.add_argument("--overwrite", action="store_true")
    sub.add_parser("live-readiness")

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    storage = Storage(settings.db_path)

    if args.command == "init-db":
        storage.init()
        print(f"Initialized database at {settings.db_path}")
        return 0

    if args.command == "doctor":
        results = Doctor(settings).run(include_network=not args.no_network)
        print(format_results(results))
        return 1 if Doctor.has_failures(results) else 0

    if args.command == "fetch-candles":
        storage.init()
        client = OkxRestClient(settings)
        for symbol in settings.symbols:
            candles = client.get_candles(symbol, settings.bar, limit=args.limit)
            storage.save_candles(candles)
            print(f"Saved {len(candles)} candles for {symbol}")
        return 0

    if args.command == "backtest":
        grouped = load_candles_csv(args.csv)
        strategy = TrendPullbackStrategy(
            ema_fast=settings.ema_fast,
            ema_slow=settings.ema_slow,
            rsi_period=settings.rsi_period,
            rsi_low=settings.rsi_low,
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
        )
        for symbol in settings.symbols:
            candles = grouped.get(symbol, [])
            if not candles:
                print(f"No candles found for {symbol}")
                continue
            result = run_backtest(
                symbol=symbol,
                candles=candles,
                strategy=strategy,
                starting_cash=args.cash,
                trade_fraction=settings.max_trade_fraction,
            )
            print(format_backtest_result(result))
        return 0

    if args.command == "run":
        runner = BotRunner(
            settings=settings,
            storage=storage,
            exchange=OkxRestClient(settings),
            notifier=Notifier(settings),
        )
        runner.run_forever()
        return 0

    if args.command == "scan-momentum":
        runner = MomentumBotRunner(
            settings=settings,
            storage=storage,
            exchange=OkxRestClient(settings),
            notifier=Notifier(settings),
        )
        scan = runner.run_once()
        if scan.best is None:
            print("No momentum candidates found.")
        else:
            print(f"Best candidate: {scan.best.symbol} score={scan.best.total_score:.2f}")
            print(scan.best.reason)
        return 0

    if args.command == "run-momentum":
        runner = MomentumBotRunner(
            settings=settings,
            storage=storage,
            exchange=OkxRestClient(settings),
            notifier=Notifier(settings),
        )
        runner.run_forever()
        return 0

    if args.command == "telegram-diagnose":
        storage.init()
        notifier = Notifier(settings)
        actions = notifier.poll_controls(storage)
        token_state = "set" if settings.telegram_bot_token else "missing"
        chat_state = "set" if settings.telegram_chat_id else "missing"
        print("Telegram diagnose")
        print(f"token={token_state}; chat_id={chat_state}; controls_enabled={settings.telegram_controls_enabled}")
        print(f"actions={actions}")
        for key in (
            "telegram_poll_status",
            "telegram_poll_started_at",
            "telegram_poll_finished_at",
            "telegram_update_offset",
            "telegram_last_update_count",
            "telegram_last_update_id",
            "telegram_last_update_text",
            "telegram_last_update_chat_id",
            "telegram_last_update_ignored_reason",
        ):
            print(f"{key}={storage.get_state(key, '')}")
        if notifier.last_error:
            print(f"last_error={notifier.last_error}")
            return 1
        return 0

    if args.command == "telegram-delete-webhook":
        notifier = Notifier(settings)
        ok = notifier.delete_webhook(drop_pending_updates=args.drop_pending_updates)
        print("Telegram deleteWebhook: OK" if ok else f"Telegram deleteWebhook: FAILED {notifier.last_error}")
        return 0 if ok else 1

    if args.command == "prepare-live-db":
        source = args.source or settings.db_path
        _prepare_live_db(source, args.dest, overwrite=args.overwrite)
        print(f"Prepared live DB at {args.dest} from {source}")
        return 0

    if args.command == "live-readiness":
        storage.init()
        report, ok = _live_readiness_report(settings)
        print(report)
        return 0 if ok else 1

    parser.error("unknown command")
    return 2


def _prepare_live_db(source: Path, dest: Path, overwrite: bool = False) -> None:
    source = source.expanduser()
    dest = dest.expanduser()
    if source.resolve() == dest.resolve():
        raise ValueError("source and dest DB must be different")
    if dest.exists() and not overwrite:
        raise ValueError(f"{dest} already exists; pass --overwrite to replace inherited experience tables")
    dest.parent.mkdir(parents=True, exist_ok=True)
    Storage(dest).init()
    tables = ("real_experiences", "experience_scores", "trade_attributions", "ai_training_runs", "market_regimes")
    with sqlite3.connect(dest) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("attach database ? as src", (str(source),))
        for table in tables:
            conn.execute(f"delete from {table}")
            dest_columns = _table_columns(conn, "main", table)
            source_columns = _table_columns(conn, "src", table)
            columns = [column for column in dest_columns if column in source_columns]
            column_csv = ", ".join(columns)
            conn.execute(f"insert into {table}({column_csv}) select {column_csv} from src.{table}")
        conn.commit()
        conn.execute("detach database src")


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"pragma {schema}.table_info({table})").fetchall()]


def _live_readiness_report(settings: Settings) -> tuple[str, bool]:
    db_path = settings.db_path
    lines = [
        "Live readiness:",
        f"DB_PATH={db_path}",
        f"TRADING_ENABLED={settings.trading_enabled} OKX_DEMO={settings.okx_demo} ALLOW_LIVE_TRADING={settings.allow_live_trading}",
        f"markets={','.join(settings.enabled_market_types)} max_open_positions={settings.max_open_positions}",
    ]
    blockers: list[str] = []
    warnings: list[str] = []

    try:
        settings.require_safe_trading_config()
    except Exception as exc:
        blockers.append(str(exc))

    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        telegram_409 = _count(conn, "select count(*) from bot_errors where source='telegram_poll' and details like '%409%'")
        errors_24h = _count(conn, "select count(*) from bot_errors where created_at >= datetime('now','-1 day')")
        swap_rejected = _count(conn, "select count(*) from orders where side='sell' and status='rejected' and symbol like '%-SWAP'")
        spot_sell_rejected = _count(conn, "select count(*) from orders where side='sell' and status='rejected' and market_type='SPOT'")
        spot_sell_filled = _count(conn, "select count(*) from orders where side='sell' and status='filled' and market_type='SPOT'")
        live_positions = _count(conn, "select count(*) from positions where base_qty > 0")
        experiences = _count(conn, "select count(*) from real_experiences")

    lines.extend(
        [
            f"real_experiences={experiences}",
            f"open_positions={live_positions}",
            f"bot_errors_24h={errors_24h}",
            f"telegram_409_errors={telegram_409}",
            f"swap_sell_rejected={swap_rejected}",
            f"spot_sell filled/rejected={spot_sell_filled}/{spot_sell_rejected}",
        ]
    )

    if telegram_409:
        blockers.append("Telegram has HTTP 409 conflicts; stop old polling instance or delete webhook before live trading.")
    if swap_rejected:
        warnings.append("SWAP sell rejects exist in demo history; strict live gate keeps live trading SPOT-only.")
    if spot_sell_rejected > max(1, spot_sell_filled):
        blockers.append("SPOT sell rejected count is too high versus filled sells.")
    if db_path == Path("data/bot.sqlite3"):
        blockers.append("Default demo DB is configured; use data/live.sqlite3 for live trading.")

    if blockers:
        lines.append("BLOCKERS:")
        lines.extend(f"- {item}" for item in blockers)
    if warnings:
        lines.append("WARNINGS:")
        lines.extend(f"- {item}" for item in warnings)
    if not blockers:
        lines.append("OK: strict live readiness gates passed.")
    return "\n".join(lines), not blockers


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0] if row else 0)
