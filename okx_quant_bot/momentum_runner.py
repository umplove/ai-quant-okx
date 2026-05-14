from __future__ import annotations

import time
from dataclasses import dataclass, replace

from okx_quant_bot.ai_reviewer import AiReviewClient
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.models import CandidateScore, OrderResult, Position, Side, StopLossOrder
from okx_quant_bot.momentum import MomentumScan, run_momentum_scan, stop_loss_plan, target_position_usdt, utc_day
from okx_quant_bot.notify import Notifier
from okx_quant_bot.trade_review import TradeReviewEngine


@dataclass
class MomentumBotRunner:
    settings: Settings
    storage: Storage
    exchange: OkxRestClient
    notifier: Notifier

    def run_forever(self) -> None:
        self.settings.require_safe_trading_config()
        self.storage.init()
        try:
            self.notifier.setup_commands()
        except Exception:
            pass
        self._send_startup_diagnostics()
        self._send_money_report()

        while True:
            try:
                self._handle_controls()
                if not self._is_paused():
                    self.run_once()
            except Exception:
                self._send_money_report()
            time.sleep(self.settings.scan_interval_seconds)

    def run_once(self) -> MomentumScan:
        self.settings.require_safe_trading_config()
        self.storage.init()
        scan = self._apply_experience_bias(run_momentum_scan(self.settings, self.exchange))
        self.storage.save_market_snapshots(scan.tickers)
        self.storage.save_info_signals(scan.info_signals)
        self.storage.save_intelligence_items(scan.intelligence_items or [])
        self.storage.save_candidate_scores(scan.candidates)
        self._review_open_trades(scan)

        ai_note = self._ai_review_text(scan)
        self._sell_positions_with_ai(scan)
        for candidate in self._ai_learning_candidates(scan):
            decision = self._ai_buy_decision(scan, candidate)
            if decision.approved_buy and self._is_tradable_candidate(candidate):
                self._buy_and_protect(candidate)

        self._send_money_report(scan=scan, ai_note=ai_note)
        return scan

    def _tradable_candidates(self, scan: MomentumScan) -> list[CandidateScore]:
        candidates: list[CandidateScore] = []
        for candidate in scan.candidates:
            if self.storage.open_position_count() + len(candidates) >= self.settings.max_open_positions:
                break
            if self._is_tradable_candidate(candidate):
                candidates.append(candidate)
        return candidates

    def _apply_experience_bias(self, scan: MomentumScan) -> MomentumScan:
        biases = self.storage.symbol_experience_biases()
        if not biases:
            return scan
        adjusted: list[CandidateScore] = []
        for candidate in scan.candidates:
            bias = biases.get(candidate.symbol, 0.0)
            if not bias:
                adjusted.append(candidate)
                continue
            adjusted.append(
                replace(
                    candidate,
                    total_score=candidate.total_score + bias,
                    reason=f"{candidate.reason}; experience_bias={bias:+.2f}",
                )
            )
        return MomentumScan(
            tickers=scan.tickers,
            info_signals=scan.info_signals,
            candidates=self._mix_experience_and_exploration(scan.candidates, adjusted),
            intelligence_items=scan.intelligence_items,
        )

    def _mix_experience_and_exploration(
        self,
        original: list[CandidateScore],
        adjusted: list[CandidateScore],
    ) -> list[CandidateScore]:
        ranked = sorted(adjusted, key=lambda c: c.total_score, reverse=True)
        exploration_slots = int(round(len(ranked) * self.settings.ai_exploration_fraction))
        exploit_slots = max(0, len(ranked) - exploration_slots)
        selected = ranked[:exploit_slots]
        seen = {candidate.symbol for candidate in selected}
        adjusted_by_symbol = {candidate.symbol: candidate for candidate in adjusted}
        for candidate in sorted(original, key=lambda c: c.total_score, reverse=True):
            if len(selected) >= len(ranked):
                break
            if candidate.symbol in seen:
                continue
            selected.append(adjusted_by_symbol[candidate.symbol])
            seen.add(candidate.symbol)
        return selected

    def _ai_learning_candidates(self, scan: MomentumScan) -> list[CandidateScore]:
        allowed = set(self.settings.symbols)
        return [
            candidate
            for candidate in scan.candidates[: self.settings.ai_review_max_candidates]
            if candidate.confirmed and candidate.symbol in allowed
        ]

    def _is_tradable_candidate(self, candidate: CandidateScore) -> bool:
        if candidate.symbol not in set(self.settings.symbols):
            return False
        if not candidate.confirmed:
            return False
        if self.storage.open_position_count() >= self.settings.max_open_positions:
            return False
        if self.storage.get_position(candidate.symbol).is_open:
            return False
        return True

    def _ai_buy_decision(self, scan: MomentumScan, candidate: CandidateScore):
        client = AiReviewClient(self.settings)
        decision = client.decide_buy(scan, candidate, self.storage.open_position_count(), self._strategy_context())
        self._record_ai_decision(candidate.symbol, "buy", decision)
        return decision

    def _ai_sell_decision(self, scan: MomentumScan, position: Position, current_price: float):
        client = AiReviewClient(self.settings)
        decision = client.decide_sell(scan, position, current_price, self._strategy_context())
        self._record_ai_decision(position.symbol, "sell", decision)
        return decision

    def _record_ai_decision(self, symbol: str, intent: str, decision) -> None:
        self.storage.save_ai_decision(
            symbol,
            intent,
            decision.action if decision.ok else "hold",
            float(decision.confidence),
            decision.reason if decision.ok else decision.error,
            decision.raw_text,
        )
        self.storage.save_ai_call_audit(
            symbol=symbol,
            intent=intent,
            ok=decision.ok,
            action=decision.action if decision.ok else "hold",
            confidence=float(decision.confidence),
            prompt_chars=int(getattr(decision, "prompt_chars", 0)),
            response_chars=int(getattr(decision, "response_chars", 0)),
            duration_ms=int(getattr(decision, "duration_ms", 0)),
            error="" if decision.ok else decision.error,
            reason=decision.reason if decision.ok else decision.error,
        )

    def _strategy_context(self) -> str:
        parts = [
            "\n".join(self.storage.recent_strategy_lessons()),
            "\n".join(self.storage.recent_trade_reviews()),
            "\n".join(self.storage.recent_intelligence(self.settings.intelligence_max_items)),
            "\n".join(self.storage.recent_ai_decisions()),
        ]
        return "\n".join(part for part in parts if part)

    def _buy_and_protect(self, candidate: CandidateScore) -> None:
        quote_amount = target_position_usdt(self.settings)
        if not self.settings.trading_enabled:
            request, result = self._dry_run_buy(candidate, quote_amount)
        else:
            request, result = self.exchange.place_market_buy_quote(
                candidate.symbol,
                quote_amount,
                f"ai_buy:{candidate.reason}",
            )
        self.storage.save_order(request, result)
        if not result.ok:
            self.storage.save_strategy_lesson(
                candidate.symbol,
                0.0,
                0.0,
                f"order_failed:{result.error or 'unknown'}",
                str(result.raw),
            )
            return

        fill_price = _filled_price(result) or candidate.price
        fill_size = _filled_size(result) or (quote_amount / fill_price if fill_price > 0 else 0.0)
        plan = stop_loss_plan(self.settings, candidate.symbol, fill_price, fill_size * fill_price)
        self.storage.save_position(Position(candidate.symbol, fill_size, fill_price, fill_price))
        stop_order = self._place_stop_loss(plan)
        self.storage.save_stop_loss_order(stop_order)
        if not stop_order.ok:
            self.storage.save_strategy_lesson(
                candidate.symbol,
                0.0,
                0.0,
                f"stop_loss_failed:{stop_order.error or 'unknown'}",
                str(stop_order.raw),
            )

    def _sell_positions_with_ai(self, scan: MomentumScan) -> None:
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in self.storage.open_positions():
            price = prices.get(position.symbol)
            if price is None:
                continue
            decision = self._ai_sell_decision(scan, position, price)
            if decision.approved_sell:
                self._sell_position(position, price, decision.reason)

    def _sell_position(self, position: Position, current_price: float, reason: str) -> None:
        if not self.settings.trading_enabled:
            request, result = self._dry_run_sell(position, reason)
        else:
            request, result = self.exchange.place_market_sell_base(
                position.symbol,
                position.base_qty,
                f"ai_sell:{reason}",
            )
        self.storage.save_order(request, result)
        if not result.ok:
            self.storage.save_strategy_lesson(
                position.symbol,
                0.0,
                0.0,
                f"sell_failed:{result.error or 'unknown'}",
                str(result.raw),
            )
            return
        pnl = (current_price - position.avg_entry_price) * position.base_qty
        return_pct = 0.0 if position.avg_entry_price <= 0 else (
            (current_price - position.avg_entry_price) / position.avg_entry_price * 100.0
        )
        self.storage.save_position(Position(symbol=position.symbol))
        self.storage.save_strategy_lesson(position.symbol, pnl, return_pct, f"ai_sell:{reason}", str(result.raw))

    def _dry_run_buy(self, candidate: CandidateScore, quote_amount: float):
        from okx_quant_bot.models import OrderRequest

        request = OrderRequest(
            symbol=candidate.symbol,
            side=Side.BUY,
            size=quote_amount,
            order_type="market",
            price=None,
            client_order_id=OkxRestClient.client_order_id("DRYB", candidate.symbol),
            reason=f"ai_buy:{candidate.reason}",
            target_currency="quote_ccy",
        )
        result = OrderResult(
            ok=True,
            symbol=candidate.symbol,
            side=Side.BUY,
            order_id="dry-run",
            client_order_id=request.client_order_id,
            raw={"dry_run": True, "avgPx": candidate.price, "accFillSz": quote_amount / candidate.price},
        )
        return request, result

    def _dry_run_sell(self, position: Position, reason: str):
        from okx_quant_bot.models import OrderRequest

        request = OrderRequest(
            symbol=position.symbol,
            side=Side.SELL,
            size=position.base_qty,
            order_type="market",
            price=None,
            client_order_id=OkxRestClient.client_order_id("DRYS", position.symbol),
            reason=f"ai_sell:{reason}",
        )
        result = OrderResult(
            ok=True,
            symbol=position.symbol,
            side=Side.SELL,
            order_id="dry-run",
            client_order_id=request.client_order_id,
            raw={"dry_run": True},
        )
        return request, result

    def _place_stop_loss(self, plan) -> StopLossOrder:
        if not self.settings.trading_enabled:
            return StopLossOrder(
                symbol=plan.symbol,
                algo_id="dry-run",
                client_order_id=OkxRestClient.client_order_id("DRYSL", plan.symbol),
                stop_price=plan.stop_price,
                size=plan.size,
                ok=True,
                raw={"dry_run": True},
            )
        return self.exchange.place_stop_loss_order(plan.symbol, plan.size, plan.stop_price)

    def _ai_review_text(self, scan: MomentumScan) -> str:
        if not self.settings.ai_review_enabled:
            return ""
        count = int(self.storage.get_state("ai_review_scan_count", "0") or "0") + 1
        self.storage.set_state("ai_review_scan_count", str(count))
        if not self.settings.ai_always_on and count % self.settings.ai_review_interval_scans != 0:
            return ""
        review = AiReviewClient(self.settings).review_scan(
            scan,
            self.storage.open_position_count(),
            self._strategy_context(),
        )
        self.storage.save_ai_call_audit(
            symbol=scan.best.symbol if scan.best else "MARKET",
            intent="scan",
            ok=review.ok,
            action="hold",
            confidence=0.0,
            prompt_chars=review.prompt_chars,
            response_chars=review.response_chars,
            duration_ms=review.duration_ms,
            error="" if review.ok else review.error,
            reason=review.text if review.ok else review.error,
        )
        if review.ok:
            return review.text
        return "" if review.error == "timeout" else f"AI unavailable: {review.error}"

    def _review_open_trades(self, scan: MomentumScan) -> None:
        if not self.settings.trade_review_enabled:
            return
        reviews = TradeReviewEngine().mark_to_market(self.storage.open_positions(), scan.tickers, note="auto review")
        for review in reviews:
            self.storage.save_trade_review(review)
            self.storage.save_strategy_lesson(
                review.symbol,
                review.pnl_usdt,
                review.return_pct,
                review.summary,
                review.raw,
            )

    def _send_money_report(
        self,
        scan: MomentumScan | None = None,
        ai_note: str = "",
        note: str = "",
    ) -> None:
        count = int(self.storage.get_state("money_report_scan_count", "0") or "0") + 1
        self.storage.set_state("money_report_scan_count", str(count))
        if scan is not None and count % self.settings.money_report_interval_scans != 0:
            return
        snapshot = self._money_snapshot()
        message = "\n".join(
            [
                f"总资产: {snapshot['equity']:.2f} USDT",
                f"今日盈亏: {snapshot['daily_pnl']:+.2f} USDT",
                f"累计盈亏: {snapshot['pnl']:+.2f} USDT",
                f"当前持仓: {self.storage.open_position_count()}/{self.settings.max_open_positions}",
                f"盈亏比: {snapshot['return_pct']:+.2f}%",
                self.storage.recent_ai_call_summary(),
            ]
        )
        if ai_note:
            message = f"{message}\nAI复盘: {ai_note}"
        if note:
            message = f"{message}\n{note}"
        self.notifier.send_money(message)
        if scan is not None:
            best = scan.best.symbol if scan.best else "NONE"
            self.storage.save_strategy_lesson(
                symbol=best,
                pnl_usdt=float(snapshot["pnl"]),
                return_pct=float(snapshot["return_pct"]),
                summary="资金快照",
                raw=message,
            )

    def _money_snapshot(self) -> dict[str, float]:
        equity = self._account_equity()
        baseline_raw = self.storage.get_state("money_baseline_equity", "")
        if not baseline_raw:
            self.storage.set_state("money_baseline_equity", str(equity))
            baseline = equity
        else:
            baseline = float(baseline_raw)
        daily_key = f"money_daily_baseline:{utc_day()}"
        daily_raw = self.storage.get_state(daily_key, "")
        if not daily_raw:
            self.storage.set_state(daily_key, str(equity))
            daily_baseline = equity
        else:
            daily_baseline = float(daily_raw)
        pnl = equity - baseline
        daily_pnl = equity - daily_baseline
        return_pct = 0.0 if baseline <= 0 else pnl / baseline * 100.0
        return {"equity": equity, "daily_pnl": daily_pnl, "pnl": pnl, "return_pct": return_pct}

    def _account_equity(self) -> float:
        if not self.settings.trading_enabled:
            return 10000.0
        payload = self.exchange.get_balance("USDT")
        data = payload.get("data", [{}])[0]
        total = data.get("totalEq")
        if total not in {None, ""}:
            return float(total)
        details = data.get("details", [])
        usdt = next((item for item in details if item.get("ccy") == "USDT"), {})
        return float(usdt.get("eq") or usdt.get("cashBal") or usdt.get("availBal") or 0)

    def _handle_controls(self) -> None:
        for action in self.notifier.poll_controls(self.storage):
            if action in {"stopped", "started", "reset", "status"}:
                self._send_money_report()

    def _is_paused(self) -> bool:
        return self.storage.get_state("bot_paused", "0") == "1"

    def _send_startup_diagnostics(self) -> None:
        mode = "demo" if self.settings.okx_demo else "live"
        trading = "on" if self.settings.trading_enabled else "off"
        ai = "on" if self.settings.ai_review_enabled else "off"
        cadence = "every scan" if self.settings.ai_always_on else f"every {self.settings.ai_review_interval_scans} scans"
        self.notifier.send_money(
            "\n".join(
                [
                    f"Startup: mode={mode}; trading={trading}; ai={ai}; ai_cadence={cadence}",
                    f"positions={self.settings.max_open_positions}; scan={self.settings.scan_interval_seconds}s",
                    f"ai_candidates=top {self.settings.ai_review_max_candidates}; exploration={self.settings.ai_exploration_fraction:.0%}",
                    f"risk_halt={'on' if self.settings.risk_halt_enabled else 'off'}; okx_skill_sources={len(self.settings.okx_skill_signal_urls)}",
                ]
            )
        )


def _filled_price(result: OrderResult) -> float | None:
    data = result.raw.get("data", [{}])
    first = data[0] if isinstance(data, list) and data else result.raw
    for key in ("avgPx", "fillPx"):
        value = first.get(key) if isinstance(first, dict) else None
        if value not in {None, ""}:
            return float(value)
    value = result.raw.get("avgPx")
    return float(value) if value not in {None, ""} else None


def _filled_size(result: OrderResult) -> float | None:
    data = result.raw.get("data", [{}])
    first = data[0] if isinstance(data, list) and data else result.raw
    for key in ("accFillSz", "fillSz"):
        value = first.get(key) if isinstance(first, dict) else None
        if value not in {None, ""}:
            return float(value)
    value = result.raw.get("accFillSz")
    return float(value) if value not in {None, ""} else None
