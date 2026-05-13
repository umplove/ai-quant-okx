from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    okx_api_key: str
    okx_secret_key: str
    okx_passphrase: str
    okx_demo: bool
    okx_base_url: str
    simulated_trading_header: bool
    trading_enabled: bool
    allow_live_trading: bool
    symbols: tuple[str, ...]
    bar: str
    db_path: Path
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_low: float
    max_trade_fraction: float
    max_symbol_fraction: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    max_daily_loss_pct: float
    max_consecutive_losses: int
    telegram_bot_token: str
    telegram_chat_id: str
    scan_interval_seconds: int = 300
    candidate_top_n: int = 20
    risk_per_trade_usdt: float = 200.0
    target_position_usdt: float = 1000.0
    stop_mode: str = "percent"
    initial_stop_loss_pct: float = 0.20
    fixed_stop_loss_usdt: float = 200.0
    max_open_positions: int = 1
    news_rss_urls: tuple[str, ...] = ()
    polymarket_enabled: bool = True
    binance_square_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        symbols = tuple(
            s.strip().upper()
            for s in os.getenv("SYMBOLS", "BTC-USDT,ETH-USDT").split(",")
            if s.strip()
        )
        return cls(
            okx_api_key=os.getenv("OKX_API_KEY", ""),
            okx_secret_key=os.getenv("OKX_SECRET_KEY", ""),
            okx_passphrase=os.getenv("OKX_PASSPHRASE", ""),
            okx_demo=_bool(os.getenv("OKX_DEMO"), True),
            okx_base_url=os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
            simulated_trading_header=_bool(os.getenv("OKX_SIMULATED_TRADING_HEADER"), True),
            trading_enabled=_bool(os.getenv("TRADING_ENABLED"), False),
            allow_live_trading=_bool(os.getenv("ALLOW_LIVE_TRADING"), False),
            symbols=symbols,
            bar=os.getenv("BAR", "1H"),
            db_path=Path(os.getenv("DB_PATH", "data/bot.sqlite3")),
            ema_fast=_int("EMA_FAST", 20),
            ema_slow=_int("EMA_SLOW", 200),
            rsi_period=_int("RSI_PERIOD", 14),
            rsi_low=_float("RSI_LOW", 35),
            max_trade_fraction=_float("MAX_TRADE_FRACTION", 0.10),
            max_symbol_fraction=_float("MAX_SYMBOL_FRACTION", 0.20),
            stop_loss_pct=_float("STOP_LOSS_PCT", 0.015),
            take_profit_pct=_float("TAKE_PROFIT_PCT", 0.03),
            trailing_stop_pct=_float("TRAILING_STOP_PCT", 0.01),
            max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 0.03),
            max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 3),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            scan_interval_seconds=_int("SCAN_INTERVAL_SECONDS", 300),
            candidate_top_n=_int("CANDIDATE_TOP_N", 20),
            risk_per_trade_usdt=_float("RISK_PER_TRADE_USDT", 200.0),
            target_position_usdt=_float("TARGET_POSITION_USDT", 1000.0),
            stop_mode=os.getenv("STOP_MODE", "percent").strip().lower(),
            initial_stop_loss_pct=_float("INITIAL_STOP_LOSS_PCT", 0.20),
            fixed_stop_loss_usdt=_float("FIXED_STOP_LOSS_USDT", 200.0),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 1),
            news_rss_urls=_csv("NEWS_RSS_URLS"),
            polymarket_enabled=_bool(os.getenv("POLYMARKET_ENABLED"), True),
            binance_square_enabled=_bool(os.getenv("BINANCE_SQUARE_ENABLED"), False),
        )

    def require_safe_trading_config(self) -> None:
        if self.trading_enabled and not self.okx_demo and not self.allow_live_trading:
            raise ValueError("Live trading is blocked unless ALLOW_LIVE_TRADING=true.")
        if self.trading_enabled and not (
            self.okx_api_key and self.okx_secret_key and self.okx_passphrase
        ):
            raise ValueError("OKX credentials are required when TRADING_ENABLED=true.")
        if not self.symbols:
            raise ValueError("At least one trading symbol must be configured.")
        if self.stop_mode not in {"percent", "fixed_loss"}:
            raise ValueError("STOP_MODE must be percent or fixed_loss.")
        if self.initial_stop_loss_pct <= 0 or self.initial_stop_loss_pct >= 1:
            raise ValueError("INITIAL_STOP_LOSS_PCT must be between 0 and 1.")
        if self.risk_per_trade_usdt <= 0 or self.fixed_stop_loss_usdt <= 0:
            raise ValueError("Risk settings must be positive.")
