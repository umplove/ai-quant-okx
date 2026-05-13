from __future__ import annotations

import argparse
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

    parser.error("unknown command")
    return 2
