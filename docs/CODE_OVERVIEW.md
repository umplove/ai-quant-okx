# Code Overview

This document explains what the OKX AI Quant Bot codebase does, how the main runtime moves from market data to orders, and where safety, AI review, and learning records fit into the system.

## 1. What This Project Does

The project is a Python trading bot for OKX. It can scan configured crypto symbols, rank short-term momentum opportunities, optionally ask an AI model for review, place guarded orders, manage exits, and store every important decision in SQLite.

The current system supports three real execution routes:

- `SPOT`: spot buy/sell using OKX `tdMode=cash`.
- `MARGIN`: spot margin long/short using `tdMode=cross` or `tdMode=isolated`.
- `SWAP`: perpetual swap long/short using leverage, `posSide`, and `reduceOnly` closing orders.

The default configuration remains conservative: spot only, demo mode first, live trading blocked unless explicit environment switches are enabled. More aggressive AI learning and leveraged experimentation can be enabled through `.env`.

## 2. Main Runtime Flow

The main command is:

```bash
python -m okx_quant_bot run-momentum
```

The command is routed through `okx_quant_bot/cli.py`, then starts `MomentumBotRunner` in `okx_quant_bot/momentum_runner.py`.

Each loop performs this sequence:

1. Validate safe trading configuration.
2. Initialize or migrate the SQLite database.
3. Pull OKX market tickers for configured symbols.
4. Build momentum candidates from price change, amplitude, volume, and public information signals.
5. Save market snapshots, intelligence items, and candidate scores.
6. Reconcile pending limit entry orders so filled orders update positions only once.
7. Check hard exits before AI sell logic.
8. Review open trades and record mark-to-market lessons.
9. Collect finished AI tasks from the background executor.
10. Submit AI market, scan, buy, and sell review tasks.
11. Execute eligible entries depending on `MOMENTUM_ENTRY_MODE`.
12. Queue background AI training and shadow-market learning.
13. Send Telegram money/status reports when configured.

The loop sleeps for `SCAN_INTERVAL_SECONDS` between cycles.

## 3. Market Scanning and Candidate Scoring

Market scanning lives in `okx_quant_bot/momentum.py`.

`MarketScanner.top_momentum_tickers()` fetches OKX spot tickers and filters to `SYMBOLS`. In spot-only mode it focuses on gainers. When margin or swap trading is enabled, it can also keep negative movers so the system can test short-side opportunities.

`CandidateScorer.score()` ranks each ticker with:

- absolute 24h price change,
- 24h amplitude,
- quote volume,
- public news / intelligence scores,
- optional information confirmation rules,
- historical experience bias from storage.

The result is a sorted list of `CandidateScore` objects. These candidates are the entry opportunity list for the runner.

## 4. Entry Modes

Entry execution is controlled by `MOMENTUM_ENTRY_MODE`.

### `ai_required`

This is the conservative mode. A candidate can only be bought or opened when:

- the candidate is confirmed,
- the symbol is configured,
- the same symbol has no open position,
- the same symbol has no pending limit buy,
- open position count is below `MAX_OPEN_POSITIONS`, or replacement is explicitly allowed,
- AI returns `action=buy`,
- AI confidence is at least `0.65`,
- `AI_EXECUTION_DECISIONS_ENABLED=true`.

### `rules_first`

This is the aggressive learning mode. Confirmed rule-based candidates can open positions without waiting for AI approval. AI still runs, but acts as:

- a high-confidence risk veto,
- a market-regime reviewer,
- an attribution engine after trades close,
- a background training source.

If `AI_RISK_VETO_ENABLED=true`, a high-confidence AI `hold` can block an entry. Otherwise, the system can continue opening rule-based positions to gather more experience.

## 5. Market Route Selection

`MomentumBotRunner._entry_market()` decides the real route:

- If `SWAP` is enabled and derivatives are allowed, candidates route to perpetual swaps.
- Else if `MARGIN` is enabled and leveraged trading is allowed, candidates route to spot margin.
- Otherwise they route to spot.

Direction is inferred from the candidate:

- Positive 24h change usually maps to `long`.
- Negative 24h change can map to `short` when `MARGIN` or `SWAP` is enabled.
- Spot always stays `long`, because real spot shorting is not possible through a normal sell without borrowed or derivative exposure.

For swaps, a spot symbol like `BTC-USDT` is converted into `BTC-USDT-SWAP` before order submission.

## 6. OKX Order Routing

OKX REST calls are in `okx_quant_bot/exchange/okx.py`.

The central order payload is represented by `OrderRequest`, and multi-market intent is represented by `TradeIntent`.

Important routes:

- `place_market_buy_quote()` for spot quote-currency market buys.
- `place_market_sell_base()` for spot base-currency market sells.
- `place_margin_market()` for margin long/short open or close.
- `place_swap_market()` for perpetual swap long/short open or close.
- `set_leverage()` before margin/swap order placement.
- `get_positions()` for authenticated OKX position reads.
- `list_open_orders()` and `get_order_details()` for pending order reconciliation.
- `place_stop_loss_order()` and `cancel_stop_loss_order()` for spot stop-loss algo orders.

Before live submission, the client reads public instrument rules and normalizes:

- `tickSz` for price steps,
- `lotSz` for size steps,
- `minSz` for minimum order size,
- `ctVal` and `ctValCcy` for swap contract sizing.

If a rounded order falls below `minSz`, the bot rejects it locally and records an execution failure instead of sending a bad payload.

## 7. Position Model

Positions are stored through `Position` in `okx_quant_bot/models.py` and persisted in the `positions` table.

Each position tracks:

- `symbol`,
- `base_qty`,
- `avg_entry_price`,
- `highest_price`,
- `market_type`,
- `direction`,
- `leverage`,
- `margin_mode`,
- `opened_at`,
- `updated_at`.

Long and short PnL are calculated differently:

- Long PnL rises when price rises.
- Short PnL rises when price falls.

The runner uses this direction-aware PnL when selling, rotating, and recording trade attribution.

## 8. Exit Logic

Hard exits run before AI sell decisions. This protects the system from waiting for AI when a configured boundary is already hit.

Configured by:

```env
MOMENTUM_EXIT_GUARD_ENABLED=true
MOMENTUM_TAKE_PROFIT_PCT=0.03
MOMENTUM_STOP_LOSS_PCT=0.02
MOMENTUM_TRAILING_STOP_PCT=0.01
MOMENTUM_MAX_HOLD_MINUTES=0
```

The runner checks:

- Hard take profit.
- Hard stop loss.
- Trailing pullback.
- Optional max holding time via `MOMENTUM_MAX_HOLD_MINUTES`.

For spot positions, active stop-loss records are replaced rather than stacked. A full exit cancels active stop-loss records.

For margin and swap positions, close orders use the opposite side and `reduceOnly` where applicable.

## 9. Rotation Logic

When `MOMENTUM_ROTATION_ENABLED=true`, the runner can replace weak positions if the book is full and a stronger candidate appears.

`MOMENTUM_ROTATION_MODE=aggressive` makes this more willing to free capacity for a new opportunity. Weakness is based primarily on current return, adjusted for short/long direction.

The goal is to avoid a stale book where old positions sit forever while new high-score opportunities are ignored.

## 10. AI Review and Training

AI integration lives mainly in `okx_quant_bot/ai_reviewer.py` and `okx_quant_bot/training.py`.

The AI can produce:

- buy/hold review,
- sell/hold review,
- entry-mode suggestions,
- exit-mode suggestions,
- market-regime classification,
- trade attribution,
- background training decisions,
- shadow-market decisions.

The runtime uses a background `ThreadPoolExecutor` so AI calls do not fully block the main trading loop. Finished decisions are saved and then used if still fresh enough.

AI provider configuration is controlled by:

```env
AI_REVIEW_ENABLED=true
OPENAI_API_KEY=
OPENAI_MODEL=mimo-v2.5-pro
OPENAI_BASE_URL=https://api.xiaomimimo.com/v1
OPENAI_API_MODE=chat
AI_REVIEW_TIMEOUT_SECONDS=12
AI_REQUEST_RETRIES=2
```

## 11. Experience Scoring

Trade attribution and experience scoring live in `okx_quant_bot/data/storage.py`.

When trades close or execution failures happen, the bot stores:

- PnL,
- return percentage,
- market type,
- direction,
- experiment cost,
- market regime,
- AI attribution category and reason,
- raw provider or exchange output.

Experience is grouped into tiers:

- `elite`: strongest repeated experience,
- `active`: usable experience,
- `cooldown`: weak or temporarily unsuitable experience,
- `rejected`: poor experience not used for active decision context,
- `archived`: reserved for long-term inactive history.

Raw trade records are preserved. The system should remove bad experience from active decision context rather than deleting audit history.

## 12. SQLite Persistence

SQLite is the single local state store. The default path is:

```env
DB_PATH=data/bot.sqlite3
```

Important tables include:

- `orders`: local and exchange order records.
- `positions`: active and recently closed position state.
- `stop_loss_orders`: active/inactive stop-loss algo records.
- `market_snapshots`: latest ticker data.
- `candidate_scores`: scored opportunities.
- `ai_decisions`: high-level AI scan decisions.
- `execution_decisions`: AI buy/sell execution JSON decisions.
- `ai_call_audits`: prompt, response, token, retry, and duration accounting.
- `shadow_decisions`: non-executing market/strategy learning records.
- `trade_attributions`: PnL and AI explanation records.
- `experience_scores`: tiered experience by symbol, market type, and direction.
- `bot_errors`: runtime errors that should be visible through Telegram or direct DB inspection.

Migration rule: SQLite `ALTER TABLE ... ADD COLUMN` must use constant defaults only. Timestamp columns should be added nullable and backfilled after creation.

## 13. Telegram Controls

Telegram integration is in `okx_quant_bot/notify.py`, and command handling is in `MomentumBotRunner._handle_controls()`.

Common commands:

- `/health`: DB, Telegram, AI, and training health.
- `/positions`: open positions with route, direction, leverage, floating PnL, and exit distance.
- `/execution`: hard-exit settings, market routes, leverage, entry mode, veto mode, and recent execution decisions.
- `/lessons`: recent attribution and experience summary.
- `/shadow`: recent shadow-market decisions.
- `/errors`: recent runtime errors.
- `/training`: AI training usage and queue status.

Telegram send failures are recorded but do not crash the bot.

## 14. Safety Switches

Important safety variables:

```env
TRADING_ENABLED=false
OKX_DEMO=true
ALLOW_LIVE_TRADING=false
ALLOW_LEVERAGED_TRADING=false
ALLOW_DERIVATIVES_TRADING=false
DERIVATIVES_DEMO_FIRST=true
RISK_HALT_ENABLED=false
```

Live non-demo trading requires `ALLOW_LIVE_TRADING=true`.

Margin trading requires `ALLOW_LEVERAGED_TRADING=true`.

Swap trading requires `ALLOW_DERIVATIVES_TRADING=true`.

If `DERIVATIVES_DEMO_FIRST=true`, live swap trading is blocked even if normal live trading is enabled. This forces an explicit decision before real derivatives execution.

## 15. Configuration Examples

Conservative spot demo:

```env
TRADING_ENABLED=true
OKX_DEMO=true
ALLOW_LIVE_TRADING=false
ENABLED_MARKET_TYPES=SPOT
MAX_OPEN_POSITIONS=5
SCAN_INTERVAL_SECONDS=300
MOMENTUM_ENTRY_MODE=ai_required
MAX_LEVERAGE=1
```

Aggressive demo learning:

```env
TRADING_ENABLED=true
OKX_DEMO=true
ALLOW_LIVE_TRADING=false
ENABLED_MARKET_TYPES=SPOT,MARGIN,SWAP
ALLOW_LEVERAGED_TRADING=true
ALLOW_DERIVATIVES_TRADING=true
DERIVATIVES_DEMO_FIRST=false
MAX_LEVERAGE=5
MAX_OPEN_POSITIONS=10
SCAN_INTERVAL_SECONDS=30
MOMENTUM_ENTRY_MODE=rules_first
AI_RISK_VETO_ENABLED=true
MOMENTUM_ROTATION_MODE=aggressive
MOMENTUM_MAX_HOLD_MINUTES=180
```

## 16. Deployment Flow

Typical server update flow:

```bash
cd ~/ai-quant-okx
sudo systemctl stop okx-quant-bot
git pull origin main
source .venv/bin/activate
python -m pip install -e .
python -m okx_quant_bot doctor --no-network
sudo systemctl restart okx-quant-bot
sudo journalctl -u okx-quant-bot -f
```

Before major upgrades, back up the SQLite database:

```bash
[ -f data/bot.sqlite3 ] && cp data/bot.sqlite3 data/bot.sqlite3.bak.$(date +%Y%m%d-%H%M%S)
```

## 17. Tests

Compile:

```bash
python -m compileall -q okx_quant_bot tests
```

Run tests:

```bash
python -m unittest discover -s tests -p "test*.py" -v
```

The tests cover configuration validation, AI parsing, storage migrations and persistence, OKX payload construction, spot/margin/swap routing, momentum entry/exit behavior, pending-order safety, Telegram controls, and training-pool accounting.

