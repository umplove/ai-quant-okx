# OKX AI Quant Bot

OKX AI Quant Bot is a Python-based cryptocurrency trading bot for OKX spot workflows. It is designed for demo trading first, with explicit guards before live trading can be enabled.

The project combines OKX market scanning, momentum scoring, optional AI review, Telegram controls, SQLite persistence, order reconciliation, and stop-loss management. It can be used as a research and automation foundation for spot trading experiments.

## Features

- OKX spot market data, balance checks, market orders, limit orders, cancellation, pending-order queries, and stop-loss algo orders.
- Demo-trading guardrails enabled by default with `OKX_DEMO=true` and `ALLOW_LIVE_TRADING=false`.
- Momentum candidate ranking from 24h change, amplitude, volume, public information signals, and historical experience.
- Short-interval spot momentum mode with default 5-minute scans and up to 5 concurrent spot positions.
- Optional AI review for buy, sell, market-regime, execution-mode, and trade-attribution decisions.
- Background AI training and shadow-market evaluation that do not block the main trading loop.
- Telegram notifications and command controls for status, AI state, positions, training, health, errors, execution decisions, lessons, and market regime.
- SQLite storage for candles, orders, positions, stop-loss orders, AI audits, strategy lessons, market intelligence, and runtime errors.
- Order safety layer for pending limit entries, filled-order reconciliation, incremental position updates, active stop-loss replacement, and OKX precision checks.
- Standard-library runtime by default; optional WebSocket support can be installed separately.

## Safety Model

The bot is conservative by default:

- `TRADING_ENABLED=false` means scans, AI calls, training, and records can run without sending orders.
- `OKX_DEMO=true` sends OKX demo-trading requests when trading is enabled.
- `ALLOW_LIVE_TRADING=false` blocks live trading startup when demo mode is off.
- Limit buy orders remain pending until OKX reports fills; repeated AI buy decisions do not create duplicate pending entries for the same symbol.
- Momentum positions use a hard exit guard by default: 3% take profit, 2% stop loss, and 1% trailing pullback protection.
- AI can suggest earlier exits or stop adjustments, but it cannot disable the hard stop-loss boundary.
- Stop-loss updates replace active stop-loss records instead of stacking multiple active stops for the same position.
- Prices and base quantities are rounded using OKX instrument metadata (`tickSz`, `lotSz`, `minSz`) before live submission.
- Real order execution is limited to OKX spot. Margin, swaps, futures, options, grids, and short-side ideas are kept as shadow learning records unless explicitly implemented later.

This repository is not investment advice. Review, test, and operate any automated trading system carefully.

## Installation

```bash
git clone https://github.com/umplove/ai-quant-okx.git
cd ai-quant-okx
python -m pip install -e .
```

Optional WebSocket dependency:

```bash
python -m pip install "websockets>=12.0"
```

## Configuration

Create a local `.env` file:

```bash
cp .env.example .env
```

Minimum OKX and runtime settings:

```env
OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=

OKX_DEMO=true
OKX_SIMULATED_TRADING_HEADER=true
TRADING_ENABLED=false
ALLOW_LIVE_TRADING=false

SYMBOLS=BTC-USDT,ETH-USDT
DB_PATH=data/bot.sqlite3
```

Optional AI review settings:

```env
AI_REVIEW_ENABLED=false
OPENAI_API_KEY=
OPENAI_MODEL=mimo-v2.5-pro
OPENAI_BASE_URL=https://api.xiaomimimo.com/v1
OPENAI_API_MODE=chat
```

Optional Telegram settings:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CONTROLS_ENABLED=true
TELEGRAM_AUTO_REPORTS=false
```

## Usage

Initialize the database:

```bash
python -m okx_quant_bot init-db
```

Run a local configuration check without network calls:

```bash
python -m okx_quant_bot doctor --no-network
```

Run a network-enabled check:

```bash
python -m okx_quant_bot doctor
```

Run one momentum scan:

```bash
python -m okx_quant_bot scan-momentum
```

Run the main momentum bot:

```bash
python -m okx_quant_bot run-momentum
```

The legacy EMA/RSI runner remains available:

```bash
python -m okx_quant_bot run
```

## Docker

Build and run with Docker Compose:

```bash
docker compose up --build
```

The Compose file mounts `./data` into the container so SQLite state persists across restarts.

## Trading Parameters

Common settings:

```env
SCAN_INTERVAL_SECONDS=300
CANDIDATE_TOP_N=20
MAX_OPEN_POSITIONS=5
TARGET_POSITION_USDT=1000
RISK_PER_TRADE_USDT=200
STOP_MODE=percent
INITIAL_STOP_LOSS_PCT=0.20
FIXED_STOP_LOSS_USDT=200
MOMENTUM_EXIT_GUARD_ENABLED=true
MOMENTUM_TAKE_PROFIT_PCT=0.03
MOMENTUM_STOP_LOSS_PCT=0.02
MOMENTUM_TRAILING_STOP_PCT=0.01
LIMIT_ORDER_ENABLED=true
SPLIT_ORDER_PARTS=3
PARTIAL_SELL_FRACTIONS=0.3,0.5,1.0
REPLACE_WEAK_POSITION_ENABLED=true
AI_EXECUTION_DECISIONS_ENABLED=true
```

Risk halt settings can be enabled for stricter operation:

```env
RISK_HALT_ENABLED=true
MAX_DAILY_LOSS_PCT=0.03
MAX_CONSECUTIVE_LOSSES=3
```

For demo learning and research, `RISK_HALT_ENABLED=false` keeps the bot collecting experience after losses. For more conservative operation, enable it.

With the defaults above, the momentum runner tries to keep scanning for new spot opportunities every 5 minutes. If there are fewer than 5 open positions and no duplicate pending entry order for a candidate, eligible symbols can continue entering while existing positions are managed independently.

The hard exit guard checks open positions before AI sell decisions:

- `MOMENTUM_TAKE_PROFIT_PCT=0.03`: sell the full spot position near +3%.
- `MOMENTUM_STOP_LOSS_PCT=0.02`: sell the full spot position near -2%.
- `MOMENTUM_TRAILING_STOP_PCT=0.01`: after a position has made a new high, sell on a 1% pullback from that high.

Telegram `/positions` includes floating PnL plus approximate distance to take-profit and stop-loss levels when a fresh market snapshot is available. `/execution` includes the active hard-exit settings.

## AI and Training

AI review can provide structured JSON decisions for:

- Buy or hold decisions for momentum candidates.
- Sell, partial sell, trail-profit, and breakeven decisions for open positions.
- Entry mode choices such as market entry, limit pullback, split limit, breakout confirmation, or wait.
- Size mode choices such as explore, reduced, normal, or strong.
- Market-regime classification and trade attribution.

AI decisions are advisory around the hard guard. They may tighten exits or suggest earlier sells, but the configured hard stop-loss and take-profit checks run first.

Training and audit records include prompt characters, response characters, prompt tokens, completion tokens, total tokens, attempted tokens, retry count, task count, success count, and error count when the provider returns those fields or when the bot can estimate attempts.

## Tests

Compile the package and tests:

```bash
python -m compileall -q okx_quant_bot tests
```

Run the test suite:

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

The suite covers AI response parsing, MiMo-compatible request bodies, storage persistence, momentum scoring, order execution modes, limit orders, split orders, partial sells, stop-loss replacement, Telegram controls, OKX client behavior, and training-pool accounting.

## Project Layout

```text
okx_quant_bot/
  ai_reviewer.py       AI request/response parsing and decision helpers
  cli.py               Command-line entrypoint
  config.py            Environment loading and safety validation
  data/storage.py      SQLite schema and persistence helpers
  exchange/okx.py      OKX REST client and order precision checks
  momentum.py          Market scanning and candidate scoring
  momentum_runner.py   Main execution loop and trade safety logic
  notify.py            Telegram notifications and controls
  risk.py              Legacy strategy risk controls
  training.py          Background AI training pool
tests/                 unittest coverage
systemd/               Example service file
```

## Operating Notes

- Keep API keys out of commits. `.env` is ignored by Git.
- Start with `TRADING_ENABLED=false` until configuration and notifications are verified.
- Prefer OKX demo trading before live use.
- Monitor `doctor`, Telegram `/health`, and stored `bot_errors` after deployment.
- If a server pulls from GitHub, deploy from a reviewed branch or from `main` after tests pass.
