from __future__ import annotations

import os
import re
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
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


def _float_csv(name: str, default: str = "") -> tuple[float, ...]:
    values: list[float] = []
    for item in os.getenv(name, default).split(","):
        item = item.strip()
        if item:
            values.append(float(item))
    return tuple(values)


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


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_value(value: str) -> str:
    return _ENV_REF.sub(lambda match: os.getenv(match.group(1), ""), value)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return _expand_env_value(value)


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
    max_open_positions: int = 5
    risk_halt_enabled: bool = False
    momentum_exit_guard_enabled: bool = True
    momentum_take_profit_pct: float = 0.03
    momentum_stop_loss_pct: float = 0.02
    momentum_trailing_stop_pct: float = 0.01
    news_rss_urls: tuple[str, ...] = ()
    news_scan_aggressive: bool = True
    intelligence_max_items: int = 30
    trade_review_enabled: bool = True
    okx_skill_signal_urls: tuple[str, ...] = ()
    polymarket_enabled: bool = True
    binance_square_enabled: bool = False
    require_info_confirmation: bool = False
    telegram_money_only: bool = True
    telegram_controls_enabled: bool = True
    telegram_auto_reports: bool = False
    money_report_interval_scans: int = 1
    openai_api_key: str = ""
    openai_model: str = "mimo-v2.5-pro"
    openai_base_url: str = "https://api.xiaomimimo.com/v1"
    openai_api_mode: str = "chat"
    ai_review_max_tokens: int = 2000
    ai_review_timeout_seconds: float = 12.0
    ai_review_enabled: bool = False
    ai_review_interval_scans: int = 1
    ai_review_max_candidates: int = 5
    ai_always_on: bool = True
    ai_exploration_fraction: float = 0.20
    ai_request_retries: int = 2
    ai_retry_backoff_seconds: float = 1.5
    ai_training_enabled: bool = True
    ai_training_workers: int = 4
    ai_weekly_token_target: int = 1_000_000_000
    ai_execution_decisions_enabled: bool = True
    limit_order_enabled: bool = True
    split_order_parts: int = 3
    partial_sell_fractions: tuple[float, ...] = (0.3, 0.5, 1.0)
    replace_weak_position_enabled: bool = True
    market_regime_enabled: bool = True
    enabled_market_types: tuple[str, ...] = ("SPOT",)
    allow_leveraged_trading: bool = False
    allow_derivatives_trading: bool = False
    derivatives_demo_first: bool = True
    margin_mode: str = "isolated"
    position_mode: str = "long_short"
    max_leverage: float = 1.0
    momentum_entry_mode: str = "ai_required"
    ai_risk_veto_enabled: bool = True
    momentum_rotation_enabled: bool = True
    momentum_rotation_mode: str = "conservative"
    momentum_max_hold_minutes: int = 0
    rotation_score_edge: float = 5.0
    experience_elite_threshold: float = 6.0
    experience_reject_threshold: float = -4.0
    experience_prune_days: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        symbols = tuple(
            s.strip().upper()
            for s in os.getenv("SYMBOLS", "BTC-USDT,ETH-USDT").split(",")
            if s.strip()
        )
        return cls(
            okx_api_key=_env("OKX_API_KEY"),
            okx_secret_key=_env("OKX_SECRET_KEY"),
            okx_passphrase=_env("OKX_PASSPHRASE"),
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
            max_open_positions=_int("MAX_OPEN_POSITIONS", 5),
            risk_halt_enabled=_bool(os.getenv("RISK_HALT_ENABLED"), False),
            momentum_exit_guard_enabled=_bool(os.getenv("MOMENTUM_EXIT_GUARD_ENABLED"), True),
            momentum_take_profit_pct=_float("MOMENTUM_TAKE_PROFIT_PCT", 0.03),
            momentum_stop_loss_pct=_float("MOMENTUM_STOP_LOSS_PCT", 0.02),
            momentum_trailing_stop_pct=_float("MOMENTUM_TRAILING_STOP_PCT", 0.01),
            news_rss_urls=_csv("NEWS_RSS_URLS"),
            news_scan_aggressive=_bool(os.getenv("NEWS_SCAN_AGGRESSIVE"), True),
            intelligence_max_items=_int("INTELLIGENCE_MAX_ITEMS", 30),
            trade_review_enabled=_bool(os.getenv("TRADE_REVIEW_ENABLED"), True),
            okx_skill_signal_urls=_csv("OKX_SKILL_SIGNAL_URLS"),
            polymarket_enabled=_bool(os.getenv("POLYMARKET_ENABLED"), False),
            binance_square_enabled=_bool(os.getenv("BINANCE_SQUARE_ENABLED"), False),
            require_info_confirmation=_bool(os.getenv("REQUIRE_INFO_CONFIRMATION"), False),
            telegram_money_only=_bool(os.getenv("TELEGRAM_MONEY_ONLY"), True),
            telegram_controls_enabled=_bool(os.getenv("TELEGRAM_CONTROLS_ENABLED"), True),
            telegram_auto_reports=_bool(os.getenv("TELEGRAM_AUTO_REPORTS"), False),
            money_report_interval_scans=_int("MONEY_REPORT_INTERVAL_SCANS", 1),
            openai_api_key=_env("OPENAI_API_KEY") or _env("MIMO_API_KEY"),
            openai_model=_env("OPENAI_MODEL", "mimo-v2.5-pro"),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/"),
            openai_api_mode=_env("OPENAI_API_MODE", "chat").strip().lower(),
            ai_review_max_tokens=_int("AI_REVIEW_MAX_TOKENS", 2000),
            ai_review_timeout_seconds=_float("AI_REVIEW_TIMEOUT_SECONDS", 12.0),
            ai_review_enabled=_bool(os.getenv("AI_REVIEW_ENABLED"), False),
            ai_review_interval_scans=_int("AI_REVIEW_INTERVAL_SCANS", 1),
            ai_review_max_candidates=_int("AI_REVIEW_MAX_CANDIDATES", 5),
            ai_always_on=_bool(os.getenv("AI_ALWAYS_ON"), True),
            ai_exploration_fraction=_float("AI_EXPLORATION_FRACTION", 0.20),
            ai_request_retries=_int("AI_REQUEST_RETRIES", 2),
            ai_retry_backoff_seconds=_float("AI_RETRY_BACKOFF_SECONDS", 1.5),
            ai_training_enabled=_bool(os.getenv("AI_TRAINING_ENABLED"), True),
            ai_training_workers=_int("AI_TRAINING_WORKERS", 4),
            ai_weekly_token_target=_int("AI_WEEKLY_TOKEN_TARGET", 1_000_000_000),
            ai_execution_decisions_enabled=_bool(os.getenv("AI_EXECUTION_DECISIONS_ENABLED"), True),
            limit_order_enabled=_bool(os.getenv("LIMIT_ORDER_ENABLED"), True),
            split_order_parts=_int("SPLIT_ORDER_PARTS", 3),
            partial_sell_fractions=_float_csv("PARTIAL_SELL_FRACTIONS", "0.3,0.5,1.0"),
            replace_weak_position_enabled=_bool(os.getenv("REPLACE_WEAK_POSITION_ENABLED"), True),
            market_regime_enabled=_bool(os.getenv("MARKET_REGIME_ENABLED"), True),
            enabled_market_types=tuple(item.upper() for item in _csv("ENABLED_MARKET_TYPES", "SPOT")),
            allow_leveraged_trading=_bool(os.getenv("ALLOW_LEVERAGED_TRADING"), False),
            allow_derivatives_trading=_bool(os.getenv("ALLOW_DERIVATIVES_TRADING"), False),
            derivatives_demo_first=_bool(os.getenv("DERIVATIVES_DEMO_FIRST"), True),
            margin_mode=os.getenv("MARGIN_MODE", "isolated").strip().lower(),
            position_mode=os.getenv("POSITION_MODE", "long_short").strip().lower(),
            max_leverage=_float("MAX_LEVERAGE", 1.0),
            momentum_entry_mode=os.getenv("MOMENTUM_ENTRY_MODE", "ai_required").strip().lower(),
            ai_risk_veto_enabled=_bool(os.getenv("AI_RISK_VETO_ENABLED"), True),
            momentum_rotation_enabled=_bool(os.getenv("MOMENTUM_ROTATION_ENABLED"), True),
            momentum_rotation_mode=os.getenv("MOMENTUM_ROTATION_MODE", "conservative").strip().lower(),
            momentum_max_hold_minutes=_int("MOMENTUM_MAX_HOLD_MINUTES", 0),
            rotation_score_edge=_float("ROTATION_SCORE_EDGE", 5.0),
            experience_elite_threshold=_float("EXPERIENCE_ELITE_THRESHOLD", 6.0),
            experience_reject_threshold=_float("EXPERIENCE_REJECT_THRESHOLD", -4.0),
            experience_prune_days=_int("EXPERIENCE_PRUNE_DAYS", 30),
        )

    def require_safe_trading_config(self) -> None:
        if self.trading_enabled and not self.okx_demo and not self.allow_live_trading:
            raise ValueError("Live trading is blocked unless ALLOW_LIVE_TRADING=true.")
        if self.trading_enabled and not (self.okx_api_key and self.okx_secret_key and self.okx_passphrase):
            raise ValueError("OKX credentials are required when TRADING_ENABLED=true.")
        allowed_markets = {"SPOT", "MARGIN", "SWAP"}
        if not self.enabled_market_types or any(market not in allowed_markets for market in self.enabled_market_types):
            raise ValueError("ENABLED_MARKET_TYPES must contain only SPOT, MARGIN, and SWAP.")
        if "MARGIN" in self.enabled_market_types and not self.allow_leveraged_trading:
            raise ValueError("MARGIN trading requires ALLOW_LEVERAGED_TRADING=true.")
        if "SWAP" in self.enabled_market_types and not self.allow_derivatives_trading:
            raise ValueError("SWAP trading requires ALLOW_DERIVATIVES_TRADING=true.")
        if self.trading_enabled and not self.okx_demo and self.derivatives_demo_first and "SWAP" in self.enabled_market_types:
            raise ValueError("Live SWAP trading is blocked unless DERIVATIVES_DEMO_FIRST=false.")
        if self.margin_mode not in {"cross", "isolated"}:
            raise ValueError("MARGIN_MODE must be cross or isolated.")
        if self.position_mode not in {"net", "long_short"}:
            raise ValueError("POSITION_MODE must be net or long_short.")
        if self.max_leverage <= 0:
            raise ValueError("MAX_LEVERAGE must be positive.")
        if self.momentum_entry_mode not in {"ai_required", "rules_first"}:
            raise ValueError("MOMENTUM_ENTRY_MODE must be ai_required or rules_first.")
        if self.momentum_rotation_mode not in {"conservative", "aggressive"}:
            raise ValueError("MOMENTUM_ROTATION_MODE must be conservative or aggressive.")
        if self.momentum_max_hold_minutes < 0:
            raise ValueError("MOMENTUM_MAX_HOLD_MINUTES must be non-negative.")
        if not self.symbols:
            raise ValueError("At least one trading symbol must be configured.")
        if self.stop_mode not in {"percent", "fixed_loss"}:
            raise ValueError("STOP_MODE must be percent or fixed_loss.")
        if self.initial_stop_loss_pct <= 0 or self.initial_stop_loss_pct >= 1:
            raise ValueError("INITIAL_STOP_LOSS_PCT must be between 0 and 1.")
        if self.risk_per_trade_usdt <= 0 or self.fixed_stop_loss_usdt <= 0:
            raise ValueError("Risk settings must be positive.")
        if self.momentum_take_profit_pct <= 0 or self.momentum_take_profit_pct >= 1:
            raise ValueError("MOMENTUM_TAKE_PROFIT_PCT must be between 0 and 1.")
        if self.momentum_stop_loss_pct <= 0 or self.momentum_stop_loss_pct >= 1:
            raise ValueError("MOMENTUM_STOP_LOSS_PCT must be between 0 and 1.")
        if self.momentum_trailing_stop_pct <= 0 or self.momentum_trailing_stop_pct >= 1:
            raise ValueError("MOMENTUM_TRAILING_STOP_PCT must be between 0 and 1.")
        if self.money_report_interval_scans <= 0:
            raise ValueError("MONEY_REPORT_INTERVAL_SCANS must be positive.")
        if self.intelligence_max_items <= 0:
            raise ValueError("INTELLIGENCE_MAX_ITEMS must be positive.")
        if self.openai_api_mode not in {"responses", "chat", "anthropic"}:
            raise ValueError("OPENAI_API_MODE must be responses, chat, or anthropic.")
        if self.ai_review_max_tokens <= 0:
            raise ValueError("AI_REVIEW_MAX_TOKENS must be positive.")
        if self.ai_review_timeout_seconds <= 0:
            raise ValueError("AI_REVIEW_TIMEOUT_SECONDS must be positive.")
        if self.ai_review_interval_scans <= 0:
            raise ValueError("AI_REVIEW_INTERVAL_SCANS must be positive.")
        if self.ai_review_max_candidates <= 0:
            raise ValueError("AI_REVIEW_MAX_CANDIDATES must be positive.")
        if self.ai_exploration_fraction < 0 or self.ai_exploration_fraction > 1:
            raise ValueError("AI_EXPLORATION_FRACTION must be between 0 and 1.")
        if self.ai_request_retries < 0:
            raise ValueError("AI_REQUEST_RETRIES must be non-negative.")
        if self.ai_retry_backoff_seconds < 0:
            raise ValueError("AI_RETRY_BACKOFF_SECONDS must be non-negative.")
        if self.ai_training_workers <= 0:
            raise ValueError("AI_TRAINING_WORKERS must be positive.")
        if self.ai_weekly_token_target <= 0:
            raise ValueError("AI_WEEKLY_TOKEN_TARGET must be positive.")
        if self.split_order_parts <= 0:
            raise ValueError("SPLIT_ORDER_PARTS must be positive.")
        if not self.partial_sell_fractions or any(value <= 0 or value > 1 for value in self.partial_sell_fractions):
            raise ValueError("PARTIAL_SELL_FRACTIONS must contain values between 0 and 1.")

    def ai_config_warning(self) -> str:
        model = self.openai_model.lower()
        base = self.openai_base_url.lower()
        if self.ai_review_enabled and (not self.openai_api_key or self.openai_api_key.startswith("${")):
            return "OPENAI_API_KEY 不是有效 key，请直接填小米 key，或设置 MIMO_API_KEY 后写 OPENAI_API_KEY=${MIMO_API_KEY}。"
        if "mimo" in model and not (self.openai_api_mode == "chat" and base == "https://api.xiaomimimo.com/v1"):
            return "MiMo 模型建议配置为 OPENAI_API_MODE=chat 且 OPENAI_BASE_URL=https://api.xiaomimimo.com/v1。"
        if model.startswith("gpt-") and "xiaomimimo" in base:
            return "OpenAI 官方模型不应使用小米 Base URL，请改用 MiMo 模型或切回 OpenAI 官方 Base URL。"
        return ""
