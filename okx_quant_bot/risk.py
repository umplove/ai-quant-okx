from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.models import Position, RiskDecision, Signal, SignalAction


@dataclass
class RiskManager:
    settings: Settings
    storage: Storage

    def can_open_position(
        self,
        signal: Signal,
        cash_balance: float,
        equity: float,
        current_position: Position,
    ) -> RiskDecision:
        if signal.action != SignalAction.BUY:
            return RiskDecision(True, "not_an_entry")
        if self._is_paused(signal.symbol):
            return RiskDecision(False, f"{signal.symbol} is paused")
        if self._consecutive_losses() >= self.settings.max_consecutive_losses:
            return RiskDecision(False, "max_consecutive_losses_reached")
        start_equity = float(self.storage.get_state(self._daily_equity_key(), str(equity)))
        self.storage.set_state(self._daily_equity_key(), str(start_equity))
        if start_equity > 0:
            daily_loss = (start_equity - equity) / start_equity
            if daily_loss >= self.settings.max_daily_loss_pct:
                return RiskDecision(False, "max_daily_loss_reached")
        if current_position.market_value(signal.price) >= equity * self.settings.max_symbol_fraction:
            return RiskDecision(False, "symbol_position_limit_reached")
        if cash_balance <= 0:
            return RiskDecision(False, "no_cash_available")
        return RiskDecision(True, "risk_ok")

    def order_size_for_entry(self, price: float, cash_balance: float, equity: float) -> float:
        budget = min(cash_balance, equity * self.settings.max_trade_fraction)
        return max(budget / price, 0.0)

    def record_trade_pnl(self, pnl: float) -> None:
        losses = self._consecutive_losses()
        if pnl < 0:
            self.storage.set_state("consecutive_losses", str(losses + 1))
        else:
            self.storage.set_state("consecutive_losses", "0")

    def pause_symbol(self, symbol: str, reason: str) -> None:
        self.storage.set_state(f"paused:{symbol}", reason)

    def _is_paused(self, symbol: str) -> bool:
        return bool(self.storage.get_state(f"paused:{symbol}", ""))

    def _consecutive_losses(self) -> int:
        return int(self.storage.get_state("consecutive_losses", "0") or 0)

    @staticmethod
    def _daily_equity_key() -> str:
        return f"daily_start_equity:{date.today().isoformat()}"

