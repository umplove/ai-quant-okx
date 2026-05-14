# AGENT.md

## Project Overview
- Python package: `okx_quant_bot`
- Purpose: OKX spot demo/live guarded quant bot with momentum scanning, AI review, Telegram controls, SQLite persistence, and trade safety checks.
- Default safety posture: demo trading is on, live trading is blocked unless `ALLOW_LIVE_TRADING=true`.

## Important Commands
- Compile: `python -m compileall -q okx_quant_bot tests`
- Tests: `python -m unittest discover -s tests -p "test*.py" -v`
- Local config check: `python -m okx_quant_bot doctor --no-network`
- Main runtime: `python -m okx_quant_bot run-momentum`
- One-shot scan: `python -m okx_quant_bot scan-momentum`

## Current Safety Architecture
- `okx_quant_bot/momentum_runner.py` owns momentum execution, pending entry order sync, position updates, and stop-loss replacement.
- `okx_quant_bot/exchange/okx.py` owns OKX REST calls, client order IDs, instrument precision checks, order placement, cancellation, and stop-loss algo orders.
- `okx_quant_bot/data/storage.py` owns SQLite schema migration and persistence for orders, positions, stop-loss orders, AI decisions, reports, and audits.
- Pending limit buy orders use local order status fields to prevent duplicate entry orders.
- Filled or partially filled limit orders are reconciled into positions by incremental filled size only.
- Active stop-loss records are replaced rather than stacked; full exits cancel active stop-loss records.
- OKX `tickSz`, `lotSz`, and `minSz` are used to round or reject live order payloads before submission.
- Momentum mode defaults to short-interval spot trading: `SCAN_INTERVAL_SECONDS=300`, `MAX_OPEN_POSITIONS=5`.
- Momentum hard exits run before AI sell decisions when enabled: `MOMENTUM_TAKE_PROFIT_PCT=0.03`, `MOMENTUM_STOP_LOSS_PCT=0.02`, `MOMENTUM_TRAILING_STOP_PCT=0.01`.
- Real execution remains spot-only. Short-side, margin, swaps, futures, options, grids, and similar ideas are shadow learning only unless explicitly implemented later.

## Git and Documentation Rules
- After any file update, update this `AGENT.md` when the change affects project behavior, workflow, or handoff context.
- After user-facing behavior changes, update `README.md` in a public, reusable style. Avoid personal or environment-specific wording.
- User preference: commit and push completed file updates to `origin/main` after verification so a server can pull the latest code.
- Do not push trading execution changes if compile/tests fail. Document failures clearly if only documentation changed.

## Notes for Future Agents
- The PowerShell terminal may display Chinese strings as mojibake, but the source files are UTF-8 and compile correctly.
- The test suite is `unittest` based. `pytest` may fail in this environment if optional dependencies are missing, so use the unittest command above for project verification.
- Do not remove demo/live guards or Telegram safe-failure behavior unless explicitly requested.
