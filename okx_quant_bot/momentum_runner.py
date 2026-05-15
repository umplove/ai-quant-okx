from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any

from okx_quant_bot.ai_reviewer import AiReviewClient
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.models import CandidateScore, OrderResult, Position, Side, StopLossOrder, TradeIntent
from okx_quant_bot.momentum import MomentumScan, run_momentum_scan, stop_loss_plan, target_position_usdt, utc_day
from okx_quant_bot.notify import Notifier
from okx_quant_bot.trade_review import TradeReviewEngine
from okx_quant_bot.training import AiTrainingPool, current_week_key


@dataclass
class MomentumBotRunner:
    settings: Settings
    storage: Storage
    exchange: OkxRestClient
    notifier: Notifier
    training_pool: AiTrainingPool | None = None
    _ai_executor: ThreadPoolExecutor | None = field(default=None, init=False, repr=False)
    _pending_ai: dict[str, Future] = field(default_factory=dict, init=False, repr=False)
    _exited_symbols: set[str] = field(default_factory=set, init=False, repr=False)
    _controls_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _controls_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _controls_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def run_forever(self) -> None:
        self.settings.require_safe_trading_config()
        self.storage.init()
        self._mark_runtime_started()
        self._save_config_snapshot()
        self._start_control_thread()
        self.notifier.setup_commands()
        if self.training_pool is None:
            self.training_pool = AiTrainingPool(self.settings, self.storage)
        self.training_pool.start()
        if self.settings.telegram_auto_reports:
            self._send_startup_diagnostics()
            self._send_money_report(force=True)

        while True:
            try:
                self._handle_controls_safely("loop_start")
                if not self._is_paused():
                    self.run_once()
            except Exception as exc:
                self.storage.save_bot_error("main_loop", "主循环异常，机器人会继续运行并等待下一轮。", traceback.format_exc())
                print(f"Main loop error: {exc}", flush=True)
                if self.settings.telegram_auto_reports and self.storage.get_state("telegram_error_reports", "0") == "1":
                    self.notifier.send(f"主循环异常，机器人会继续运行并等待下一轮: {exc}")
            self._sleep_with_controls(self.settings.scan_interval_seconds)

    def run_once(self) -> MomentumScan:
        self._exited_symbols = set()
        self.settings.require_safe_trading_config()
        self.storage.init()
        self._ensure_runtime_started()
        scan = self._apply_experience_bias(run_momentum_scan(self.settings, self.exchange))
        self._handle_controls_safely("after_scan")
        self.storage.save_market_snapshots(scan.tickers)
        self.storage.save_info_signals(scan.info_signals)
        self.storage.save_intelligence_items(scan.intelligence_items or [])
        self.storage.save_candidate_scores(scan.candidates)
        self._handle_controls_safely("after_snapshot")
        sync_note = self._sync_exchange_state(scan)
        self._handle_controls_safely("after_okx_sync")
        self._sync_pending_entry_orders(scan)
        self._handle_controls_safely("after_order_sync")
        self._sell_positions_with_hard_exit(scan)
        self._handle_controls_safely("after_hard_exit")
        self._review_open_trades(scan)
        self._handle_controls_safely("after_trade_review")

        self._collect_finished_ai_tasks()
        market_regime = self._current_market_regime()
        ai_note = self._latest_ai_review_text(scan)
        self._sell_positions_with_recent_ai(scan)
        self._handle_controls_safely("after_ai_sell_apply")
        self._submit_market_regime(scan)
        self._submit_ai_review(scan)
        self._submit_sell_reviews(scan, market_regime)
        self._handle_controls_safely("after_ai_submit")
        self._collect_finished_ai_tasks(grace_seconds=0.05)
        self._sell_positions_with_recent_ai(scan)
        self._handle_controls_safely("after_ai_collect")
        self._handle_controls_safely("before_ai_buy")
        for candidate in self._ai_learning_candidates(scan):
            self._submit_buy_review(scan, candidate, market_regime)
            self._collect_finished_ai_tasks(grace_seconds=0.05)
            decision = self.storage.latest_execution_decision(candidate.symbol, "buy", self._ai_decision_ttl_seconds())
            decision = self._entry_decision(candidate, decision)
            if decision and self._is_tradable_or_replaceable(candidate, decision):
                self._execute_buy_decision(candidate, decision, scan)
        self._handle_controls_safely("after_ai_buy")

        if self.training_pool is not None:
            self.training_pool.enqueue_scan(scan, self._strategy_context(), self.storage.open_positions())
        self._handle_controls_safely("after_training_enqueue")
        self._send_money_report(scan=scan, ai_note=ai_note, note=sync_note)
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
        open_count = self.storage.open_position_count()
        available_slots = max(0, self.settings.max_open_positions - open_count)
        if available_slots <= 0:
            self.storage.set_state("last_buy_ai_skip_reason", "持仓已满，只做持仓管理和卖出/换仓判断")
            return []
        review_limit = min(self.settings.ai_review_max_candidates, available_slots + 1)
        self.storage.set_state(
            "last_buy_ai_skip_reason",
            f"当前持仓{open_count}/{self.settings.max_open_positions}，买入AI最多审核{review_limit}个候选",
        )
        return [
            candidate
            for candidate in scan.candidates[:review_limit]
            if candidate.confirmed and candidate.symbol in allowed
        ]

    def _is_tradable_candidate(self, candidate: CandidateScore) -> bool:
        if candidate.symbol not in set(self.settings.symbols):
            return False
        if candidate.symbol in self._exited_symbols:
            return False
        if not candidate.confirmed:
            return False
        if self.storage.pending_entry_orders(candidate.symbol):
            return False
        if self.storage.open_position_count() >= self.settings.max_open_positions:
            return False
        if self.storage.get_position(candidate.symbol).is_open:
            return False
        return True

    def _entry_decision(self, candidate: CandidateScore, decision: dict | None) -> dict | None:
        if self.settings.momentum_entry_mode != "rules_first":
            return decision
        if decision and self._ai_vetoes_entry(decision):
            self.storage.save_strategy_lesson(candidate.symbol, 0.0, 0.0, f"ai_veto_buy:{decision.get('reason') or ''}", "")
            self.storage.save_execution_event(
                candidate.symbol,
                "SPOT",
                "long",
                "buy",
                "entry_decision",
                "blocked",
                "ai_risk_veto",
                reason=str(decision.get("reason") or ""),
            )
            self.storage.save_real_experience(
                candidate.symbol,
                "SPOT",
                "long",
                self._current_market_regime(),
                "buy",
                "risk_rejected",
                confidence=float(decision.get("confidence") or 0.0),
                reason=str(decision.get("reason") or ""),
                source="ai_risk_veto",
            )
            return None
        if decision and str(decision.get("action") or "") == "buy":
            return decision
        return {
            "action": "buy",
            "entry_mode": "market_now",
            "exit_mode": "hold",
            "size_mode": "normal",
            "stop_mode": "fixed",
            "replace_mode": "replace_weakest" if self.settings.momentum_rotation_mode == "aggressive" else "none",
            "confidence": 0.66,
            "reason": f"rules_first_buy:{candidate.reason}",
        }

    def _ai_vetoes_entry(self, decision: dict) -> bool:
        if not self.settings.ai_risk_veto_enabled:
            return False
        return str(decision.get("action") or "") == "hold" and float(decision.get("confidence") or 0.0) >= 0.85

    def _is_tradable_or_replaceable(self, candidate: CandidateScore, decision: dict) -> bool:
        if not self._execution_buy_allowed(decision):
            return False
        if self._is_tradable_candidate(candidate):
            return True
        return (
            self.settings.replace_weak_position_enabled
            and self.settings.momentum_rotation_enabled
            and self.storage.open_position_count() >= self.settings.max_open_positions
            and str(decision.get("replace_mode")) == "replace_weakest"
            and not self.storage.get_position(candidate.symbol).is_open
        )

    def _ensure_ai_executor(self) -> ThreadPoolExecutor:
        if self._ai_executor is None:
            workers = max(2, min(8, self.settings.ai_training_workers))
            self._ai_executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai-trade")
        return self._ai_executor

    def _submit_ai_task(self, key: str, fn, *args) -> None:
        self._collect_finished_ai_tasks()
        pending = self._pending_ai.get(key)
        if pending is not None and not pending.done():
            return
        self._pending_ai[key] = self._ensure_ai_executor().submit(fn, *args)

    def _collect_finished_ai_tasks(self, grace_seconds: float = 0.0) -> None:
        if grace_seconds > 0:
            for future in list(self._pending_ai.values()):
                if future.done():
                    continue
                try:
                    future.result(timeout=grace_seconds)
                except FutureTimeout:
                    pass
                except Exception:
                    pass
        finished = [key for key, future in self._pending_ai.items() if future.done()]
        for key in finished:
            future = self._pending_ai.pop(key)
            try:
                future.result()
            except Exception as exc:
                self.storage.save_bot_error("ai_decision_task", f"AI后台决策任务失败: {key}", str(exc))

    def _submit_ai_review(self, scan: MomentumScan) -> None:
        count = int(self.storage.get_state("ai_review_scan_count", "0") or "0") + 1
        self.storage.set_state("ai_review_scan_count", str(count))
        if not self.settings.ai_always_on and count % self.settings.ai_review_interval_scans != 0:
            return
        self._submit_ai_task("scan:MARKET", self._ai_review_text, scan)

    def _submit_market_regime(self, scan: MomentumScan) -> None:
        if self.settings.market_regime_enabled:
            self._submit_ai_task("market_regime:MARKET", self._ai_market_regime_decision, scan)

    def _submit_buy_review(self, scan: MomentumScan, candidate: CandidateScore, market_regime: str) -> None:
        self._submit_ai_task(f"buy:{candidate.symbol}", self._ai_buy_decision, scan, candidate, market_regime)

    def _submit_sell_reviews(self, scan: MomentumScan, market_regime: str) -> None:
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in self.storage.open_positions():
            price = prices.get(position.symbol)
            if price is None:
                continue
            self._submit_ai_task(f"sell:{position.symbol}", self._ai_sell_decision, scan, position, price, market_regime)

    def _ai_decision_ttl_seconds(self) -> int:
        return max(60, int(self.settings.scan_interval_seconds * 3))

    def _latest_ai_review_text(self, scan: MomentumScan) -> str:
        symbol = scan.best.symbol if scan.best else "MARKET"
        decision = self.storage.latest_ai_decision(symbol, "scan", self._ai_decision_ttl_seconds())
        if not decision:
            return ""
        return str(decision.get("reason") or "")[:500]

    def _current_market_regime(self) -> str:
        regime = self.storage.latest_market_regime()
        return str(regime.get("regime")) if regime else "未知"

    def _ai_buy_decision(self, scan: MomentumScan, candidate: CandidateScore, market_regime: str = ""):
        client = AiReviewClient(self.settings)
        decision = client.decide_buy(
            scan,
            candidate,
            self.storage.open_position_count(),
            self._strategy_context(),
            market_regime,
        )
        self._record_ai_decision(candidate.symbol, "buy", decision)
        return decision

    def _ai_sell_decision(self, scan: MomentumScan, position: Position, current_price: float, market_regime: str = ""):
        client = AiReviewClient(self.settings)
        decision = client.decide_sell(scan, position, current_price, self._strategy_context(), market_regime)
        self._record_ai_decision(position.symbol, "sell", decision)
        return decision

    def _ai_market_regime_decision(self, scan: MomentumScan) -> None:
        regime = AiReviewClient(self.settings).decide_market_regime(scan, self._strategy_context())
        regime_ok = getattr(regime, "ok", False) is True
        if regime_ok:
            self.storage.save_market_regime(regime.regime, regime.confidence, regime.reason, regime.raw_text)
        else:
            error = getattr(regime, "error", "") if isinstance(getattr(regime, "error", ""), str) else "invalid market regime response"
            raw_text = getattr(regime, "raw_text", "") if isinstance(getattr(regime, "raw_text", ""), str) else ""
            self.storage.save_bot_error("market_regime", "AI行情状态判断失败", error)
            self.storage.save_real_experience(
                "MARKET",
                "SPOT",
                "long",
                "",
                "market_regime",
                "json_failed" if "parse_failed" in error else "failed",
                confidence=0.0,
                reason=error,
                source="market_regime",
                raw=raw_text,
            )
        self.storage.save_ai_call_audit(
            symbol="MARKET",
            intent="market_regime",
            ok=regime_ok,
            action="hold",
            confidence=float(getattr(regime, "confidence", 0.0) if isinstance(getattr(regime, "confidence", 0.0), (int, float)) else 0.0),
            prompt_chars=_int_attr(regime, "prompt_chars"),
            response_chars=_int_attr(regime, "response_chars"),
            duration_ms=_int_attr(regime, "duration_ms"),
            error="" if regime_ok else error,
            reason=regime.reason if regime_ok else error,
            prompt_tokens=_int_attr(regime, "prompt_tokens"),
            completion_tokens=_int_attr(regime, "completion_tokens"),
            total_tokens=_int_attr(regime, "total_tokens"),
            attempted_tokens=_int_attr(regime, "attempted_tokens"),
            retry_count=_int_attr(regime, "retry_count"),
        )

    def _record_ai_decision(self, symbol: str, intent: str, decision) -> None:
        action = decision.action if decision.ok else "hold"
        reason = decision.reason if decision.ok else decision.error
        entry_mode = getattr(decision, "entry_mode", "wait")
        exit_mode = getattr(decision, "exit_mode", "hold")
        if action == "buy" and entry_mode == "wait":
            entry_mode = "market_now"
        if action == "sell" and exit_mode == "hold":
            exit_mode = "sell_all"
        self.storage.save_ai_decision(symbol, intent, action, float(decision.confidence), reason, decision.raw_text)
        self.storage.save_execution_decision(
            symbol=symbol,
            intent=intent,
            action=action,
            entry_mode=entry_mode,
            exit_mode=exit_mode,
            size_mode=getattr(decision, "size_mode", "normal"),
            stop_mode=getattr(decision, "stop_mode", "fixed"),
            replace_mode=getattr(decision, "replace_mode", "none"),
            confidence=float(decision.confidence),
            reason=reason,
            raw=decision.raw_text,
        )
        self.storage.save_ai_call_audit(
            symbol=symbol,
            intent=intent,
            ok=decision.ok,
            action=action,
            confidence=float(decision.confidence),
            prompt_chars=int(getattr(decision, "prompt_chars", 0)),
            response_chars=int(getattr(decision, "response_chars", 0)),
            duration_ms=int(getattr(decision, "duration_ms", 0)),
            error="" if decision.ok else decision.error,
            reason=reason,
            prompt_tokens=int(getattr(decision, "prompt_tokens", 0)),
            completion_tokens=int(getattr(decision, "completion_tokens", 0)),
            total_tokens=int(getattr(decision, "total_tokens", 0)),
            attempted_tokens=int(getattr(decision, "attempted_tokens", 0)),
            retry_count=int(getattr(decision, "retry_count", 0)),
        )
        if not decision.ok and "parse_failed" in str(decision.error):
            self.storage.save_real_experience(
                symbol,
                "SPOT",
                "long",
                self._current_market_regime(),
                intent,
                "json_failed",
                confidence=0.0,
                reason=decision.error,
                source=f"{intent}_parse_failed",
                raw=decision.raw_text,
            )

    def _strategy_context(self) -> str:
        parts = [
            "\n".join(self.storage.recent_real_experiences(limit=16)),
            "\n".join(self.storage.recent_strategy_lessons()),
            "\n".join(self.storage.recent_trade_reviews()),
            "\n".join(self.storage.recent_intelligence(self.settings.intelligence_max_items)),
            "\n".join(self.storage.recent_ai_decisions()),
            "\n".join(self.storage.recent_trade_attributions(limit=8)),
            "\n".join(self.storage.recent_market_regimes(limit=3)),
        ]
        return "\n".join(part for part in parts if part)

    def _execute_buy_decision(self, candidate: CandidateScore, decision: dict, scan: MomentumScan) -> None:
        if not self._execution_buy_allowed(decision):
            self.storage.save_execution_event(
                candidate.symbol,
                "SPOT",
                "long",
                "buy",
                "entry_decision",
                "blocked",
                "execution_guard",
                reason=str(decision.get("reason") or "buy not allowed"),
            )
            return
        if self.storage.pending_entry_orders(candidate.symbol):
            self.storage.save_strategy_lesson(candidate.symbol, 0.0, 0.0, "execution_wait:pending_entry_order", "")
            self.storage.save_execution_event(
                candidate.symbol,
                "SPOT",
                "long",
                "buy",
                "entry_decision",
                "pending_entry_order",
                "execution_guard",
                reason="已有 pending 限价入场单，跳过重复下单",
            )
            return
        if self.storage.open_position_count() >= self.settings.max_open_positions:
            if str(decision.get("replace_mode")) != "replace_weakest":
                self.storage.save_execution_event(
                    candidate.symbol,
                    "SPOT",
                    "long",
                    "buy",
                    "entry_decision",
                    "blocked",
                    "execution_guard",
                    reason="持仓已满且未允许换仓",
                )
                return
            if not self._replace_weakest_position(scan, f"换仓买入{candidate.symbol}: {decision.get('reason') or ''}"):
                self.storage.save_execution_event(
                    candidate.symbol,
                    "SPOT",
                    "long",
                    "buy",
                    "entry_decision",
                    "blocked",
                    "execution_guard",
                    reason="换仓卖出未完成，跳过买入",
                )
                return

        entry_mode = str(decision.get("entry_mode") or "market_now")
        quote_amount = self._target_quote_amount(str(decision.get("size_mode") or "normal"))
        reason = str(decision.get("reason") or candidate.reason)
        if entry_mode in {"wait", "breakout_confirm"}:
            self.storage.save_strategy_lesson(candidate.symbol, 0.0, 0.0, f"execution_wait:{entry_mode}:{reason}", "")
            self.storage.save_execution_event(
                candidate.symbol,
                "SPOT",
                "long",
                "buy",
                "entry_decision",
                entry_mode,
                "execution_guard",
                reason=reason,
            )
            return
        if str(reason).startswith("rules_first_buy:"):
            self.storage.save_execution_event(
                candidate.symbol,
                "SPOT",
                "long",
                "buy",
                "entry_decision",
                "approved",
                "rules_decision",
                reason=reason,
            )
        if entry_mode == "limit_pullback" and self.settings.limit_order_enabled:
            self._place_limit_buy(candidate, quote_amount, candidate.price * 0.997, reason)
            return
        if entry_mode == "split_limit" and self.settings.limit_order_enabled:
            self._place_split_limit_buys(candidate, quote_amount, reason)
            return
        self._open_candidate_position(candidate, quote_amount, reason)

    def _execution_buy_allowed(self, decision: dict) -> bool:
        return (
            str(decision.get("action") or "") == "buy"
            and float(decision.get("confidence") or 0.0) >= 0.65
            and self.settings.ai_execution_decisions_enabled
        )

    def _target_quote_amount(self, size_mode: str) -> float:
        multipliers = {"explore": 0.3, "reduced": 0.5, "normal": 1.0, "strong": 1.5}
        return target_position_usdt(self.settings) * multipliers.get(size_mode, 1.0)

    def _open_candidate_position(self, candidate: CandidateScore, quote_amount: float, reason: str) -> None:
        market_type, direction = self._entry_market(candidate)
        if market_type == "SPOT":
            self._buy_and_protect(candidate, quote_amount, reason)
            return
        trade_symbol = self._trade_symbol(candidate.symbol, market_type)
        leverage = self.settings.max_leverage
        margin_mode = self.settings.margin_mode
        side = Side.SELL if direction == "short" else Side.BUY
        if not self.settings.trading_enabled:
            request, result = self._dry_run_buy(candidate, quote_amount, "market")
            request = request.__class__(
                **{
                    **request.__dict__,
                    "symbol": trade_symbol,
                    "side": side,
                    "market_type": market_type,
                    "direction": direction,
                    "td_mode": margin_mode,
                    "pos_side": direction if market_type == "SWAP" else None,
                    "leverage": leverage,
                }
            )
        elif market_type == "MARGIN":
            base_size = quote_amount / candidate.price if candidate.price > 0 else 0.0
            request, result = self.exchange.place_margin_market(
                trade_symbol,
                side,
                base_size,
                direction,
                leverage,
                margin_mode,
                f"rules_margin:{reason}",
            )
        else:
            contract_size = self.exchange.swap_contract_size_for_quote(trade_symbol, quote_amount, candidate.price)
            request, result = self.exchange.place_swap_market(
                trade_symbol,
                side,
                contract_size,
                direction,
                leverage,
                margin_mode,
                f"rules_swap:{reason}",
            )
        self.storage.save_order(request, result)
        if not result.ok:
            self._record_execution_failure(trade_symbol, "entry_failed", result.error or "unknown", result.raw)
            return
        fill_price = _filled_price(result) or candidate.price
        fill_size = _filled_size(result) or (
            self.exchange.swap_contract_size_for_quote(trade_symbol, quote_amount, fill_price)
            if market_type == "SWAP" and self.settings.trading_enabled
            else quote_amount / fill_price if fill_price > 0 else 0.0
        )
        self.storage.save_position(Position(trade_symbol, fill_size, fill_price, fill_price, market_type, direction, leverage, margin_mode))

    def _entry_market(self, candidate: CandidateScore) -> tuple[str, str]:
        markets = set(self.settings.enabled_market_types)
        direction = "short" if candidate.change_pct_24h < 0 and markets.intersection({"MARGIN", "SWAP"}) else "long"
        if "SWAP" in markets and self.settings.allow_derivatives_trading:
            return "SWAP", direction
        if "MARGIN" in markets and self.settings.allow_leveraged_trading:
            return "MARGIN", direction
        return "SPOT", "long"

    def _trade_symbol(self, symbol: str, market_type: str) -> str:
        if market_type == "SWAP" and not symbol.endswith("-SWAP"):
            return f"{symbol}-SWAP"
        return symbol

    def _buy_and_protect(self, candidate: CandidateScore, quote_amount: float | None = None, reason: str | None = None) -> None:
        quote_amount = quote_amount if quote_amount is not None else target_position_usdt(self.settings)
        reason = reason or candidate.reason
        if not self.settings.trading_enabled:
            request, result = self._dry_run_buy(candidate, quote_amount, "market")
        else:
            request, result = self.exchange.place_market_buy_quote(candidate.symbol, quote_amount, f"ai_buy:{reason}")
        self.storage.save_order(request, result)
        if not result.ok:
            self._record_execution_failure(candidate.symbol, "买入失败", result.error or "unknown", result.raw)
            return

        fill_price = _filled_price(result) or candidate.price
        fill_size = _filled_size(result) or (quote_amount / fill_price if fill_price > 0 else 0.0)
        plan = stop_loss_plan(self.settings, candidate.symbol, fill_price, fill_size * fill_price)
        position = Position(candidate.symbol, fill_size, fill_price, fill_price)
        self.storage.save_position(position)
        stop_order = self._replace_stop_loss(position, plan.stop_price)
        if not stop_order.ok:
            self._record_execution_failure(candidate.symbol, "止损单失败", stop_order.error or "unknown", stop_order.raw)

    def _place_limit_buy(self, candidate: CandidateScore, quote_amount: float, price: float, reason: str) -> None:
        if not self.settings.trading_enabled:
            request, result = self._dry_run_limit_buy(candidate, quote_amount, price, reason)
        else:
            request, result = self.exchange.place_limit_buy_quote(candidate.symbol, quote_amount, price, f"ai_limit_buy:{reason}")
        self.storage.save_order(request, result)
        if not result.ok:
            self._record_execution_failure(candidate.symbol, "限价买入失败", result.error or "unknown", result.raw)

    def _place_split_limit_buys(self, candidate: CandidateScore, quote_amount: float, reason: str) -> None:
        parts = max(1, self.settings.split_order_parts)
        part_amount = quote_amount / parts
        for idx in range(parts):
            price = candidate.price * (1 - 0.003 * (idx + 1))
            self._place_limit_buy(candidate, part_amount, price, f"{reason}; split={idx + 1}/{parts}")

    def _sell_positions_with_recent_ai(self, scan: MomentumScan) -> None:
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in self.storage.open_positions():
            price = prices.get(position.symbol)
            if price is None:
                continue
            decision = self.storage.latest_execution_decision(position.symbol, "sell", self._ai_decision_ttl_seconds())
            if not decision:
                continue
            if str(decision.get("action")) != "sell" or float(decision.get("confidence") or 0.0) < 0.65:
                continue
            exit_mode = str(decision.get("exit_mode") or "sell_all")
            reason = str(decision.get("reason") or "AI建议卖出")
            if exit_mode == "sell_partial":
                self._sell_position(position, price, reason, fraction=self.settings.partial_sell_fractions[0])
            elif exit_mode == "trail_profit":
                self._move_stop(position, price * (1 - self.settings.trailing_stop_pct), "AI追踪止盈")
            elif exit_mode == "move_to_breakeven":
                self._move_stop(position, position.avg_entry_price, "AI保本止损")
            elif exit_mode == "sell_all":
                self._sell_position(position, price, reason, fraction=1.0)

    def _sell_positions_with_ai(self, scan: MomentumScan) -> None:
        self._sell_positions_with_recent_ai(scan)

    def _sell_positions_with_hard_exit(self, scan: MomentumScan) -> None:
        if not self.settings.momentum_exit_guard_enabled:
            return
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in self.storage.open_positions():
            price = prices.get(position.symbol) or prices.get(_spot_symbol(position.symbol))
            if price is None or price <= 0 or position.avg_entry_price <= 0:
                continue
            if self.settings.momentum_max_hold_minutes > 0:
                age = self.storage.position_age_minutes(position.symbol)
                if age >= self.settings.momentum_max_hold_minutes:
                    self._sell_position(position, price, "time_rotation_exit", fraction=1.0)
                    continue
            prior_highest = max(position.highest_price, position.avg_entry_price)
            if price > prior_highest:
                position = Position(position.symbol, position.base_qty, position.avg_entry_price, price)
                self.storage.save_position(position)
                prior_highest = price
            if position.direction == "short":
                take_profit_price = position.avg_entry_price * (1.0 - self.settings.momentum_take_profit_pct)
                stop_loss_price = position.avg_entry_price * (1.0 + self.settings.momentum_stop_loss_pct)
                trailing_price = prior_highest * (1.0 + self.settings.momentum_trailing_stop_pct)
                take_profit_hit = price <= take_profit_price
                stop_loss_hit = price >= stop_loss_price
                trailing_hit = prior_highest < position.avg_entry_price and price >= trailing_price
            else:
                take_profit_price = position.avg_entry_price * (1.0 + self.settings.momentum_take_profit_pct)
                stop_loss_price = position.avg_entry_price * (1.0 - self.settings.momentum_stop_loss_pct)
                trailing_price = prior_highest * (1.0 - self.settings.momentum_trailing_stop_pct)
                take_profit_hit = price >= take_profit_price
                stop_loss_hit = price <= stop_loss_price
                trailing_hit = prior_highest > position.avg_entry_price and price <= trailing_price
            if take_profit_hit:
                self._sell_position(position, price, "hard_take_profit", fraction=1.0)
            elif stop_loss_hit:
                self._sell_position(position, price, "hard_stop_loss", fraction=1.0)
            elif trailing_hit:
                self._sell_position(position, price, "hard_trailing_stop", fraction=1.0)

    def _sell_position(self, position: Position, current_price: float, reason: str, fraction: float = 1.0) -> None:
        sell_qty = position.base_qty * max(0.0, min(fraction, 1.0))
        if sell_qty <= 0:
            return
        stop_price = self._current_stop_price(position)
        if not self.settings.trading_enabled:
            request, result = self._dry_run_sell(position, reason, sell_qty)
        elif position.market_type == "MARGIN":
            request, result = self.exchange.place_margin_market(
                position.symbol,
                Side.BUY if position.direction == "short" else Side.SELL,
                sell_qty,
                position.direction,
                position.leverage,
                position.margin_mode,
                f"close_margin:{reason}",
                reduce_only=True,
            )
        elif position.market_type == "SWAP":
            request, result = self.exchange.place_swap_market(
                position.symbol,
                Side.BUY if position.direction == "short" else Side.SELL,
                sell_qty,
                position.direction,
                position.leverage,
                position.margin_mode,
                f"close_swap:{reason}",
                reduce_only=True,
            )
        else:
            request, result = self.exchange.place_market_sell_base(position.symbol, sell_qty, f"ai_sell:{reason}")
        self.storage.save_order(request, result)
        if not result.ok:
            self._record_execution_failure(position.symbol, "卖出失败", result.error or "unknown", result.raw)
            return
        multiplier = -1.0 if position.direction == "short" else 1.0
        pnl = (current_price - position.avg_entry_price) * sell_qty * multiplier
        return_pct = 0.0 if position.avg_entry_price <= 0 else (
            (current_price - position.avg_entry_price) / position.avg_entry_price * multiplier * 100.0
        )
        remaining = position.base_qty - sell_qty
        if remaining > 1e-12:
            updated = Position(
                position.symbol,
                remaining,
                position.avg_entry_price,
                max(position.highest_price, current_price),
                position.market_type,
                position.direction,
                position.leverage,
                position.margin_mode,
            )
            self.storage.save_position(updated)
            if position.market_type == "SPOT":
                self._replace_stop_loss(updated, stop_price)
        else:
            self.storage.save_position(Position(symbol=position.symbol))
            self._cancel_active_stop_losses(position.symbol)
            self._exited_symbols.add(_spot_symbol(position.symbol))
        self.storage.save_strategy_lesson(position.symbol, pnl, return_pct, f"ai_sell:{reason}", str(result.raw), position.market_type, position.direction)
        self._record_trade_attribution(position.symbol, pnl, return_pct, f"卖出完成: {reason}", position)

    def _replace_weakest_position(self, scan: MomentumScan, reason: str) -> bool:
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        weakest: tuple[float, Position, float] | None = None
        for position in self.storage.open_positions():
            price = prices.get(position.symbol) or prices.get(_spot_symbol(position.symbol), position.avg_entry_price)
            multiplier = -1.0 if position.direction == "short" else 1.0
            return_pct = 0.0 if position.avg_entry_price <= 0 else (price - position.avg_entry_price) / position.avg_entry_price * multiplier * 100.0
            if weakest is None or return_pct < weakest[0]:
                weakest = (return_pct, position, price)
        if weakest is None:
            return False
        _, position, price = weakest
        self._sell_position(position, price, reason, fraction=1.0)
        return not self.storage.get_position(position.symbol).is_open

    def _move_stop(self, position: Position, stop_price: float, reason: str) -> None:
        stop_order = self._replace_stop_loss(position, stop_price)
        if stop_order.ok:
            self.storage.save_strategy_lesson(position.symbol, 0.0, 0.0, f"{reason}: stop={stop_price:.8g}", str(stop_order.raw))
        else:
            self._record_execution_failure(position.symbol, f"{reason}失败", stop_order.error or "unknown", stop_order.raw)

    def _sync_pending_entry_orders(self, scan: MomentumScan) -> None:
        if not self.settings.trading_enabled:
            return
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for order in self.storage.pending_entry_orders():
            order_id = str(order.get("exchange_order_id") or "")
            if not order_id:
                continue
            symbol = str(order["symbol"])
            try:
                payload = self.exchange.get_order_details(symbol, order_id)
            except Exception as exc:
                self.storage.save_bot_error("order_sync", f"{symbol} order sync failed", str(exc))
                self.storage.save_execution_event(
                    symbol,
                    str(order.get("market_type") or "SPOT"),
                    str(order.get("direction") or "long"),
                    str(order.get("side") or "buy"),
                    "order_sync",
                    "failed",
                    "okx_order_sync",
                    client_order_id=str(order.get("client_order_id") or ""),
                    exchange_order_id=order_id,
                    order_type=str(order.get("order_type") or ""),
                    reason="get_order_details failed",
                    error=str(exc),
                )
                continue
            state = _order_state(payload)
            filled_size = _filled_size_from_order_payload(payload)
            avg_price = _filled_price_from_order_payload(payload) or float(order.get("avg_fill_price") or 0.0)
            previous_size = float(order.get("filled_size") or 0.0)
            delta_size = max(0.0, filled_size - previous_size)
            if delta_size > 0:
                fill_price = avg_price or prices.get(symbol) or float(order.get("price") or 0.0)
                self._apply_entry_fill(symbol, delta_size, fill_price)
            if state:
                self.storage.update_order_fill(
                    str(order["client_order_id"]),
                    state,
                    filled_size,
                    avg_price if avg_price > 0 else None,
                    raw=payload,
                    exchange_order_id=order_id,
                )

    def _apply_entry_fill(self, symbol: str, fill_size: float, fill_price: float) -> None:
        if fill_size <= 0 or fill_price <= 0:
            return
        current = self.storage.get_position(symbol)
        if current.is_open:
            total_size = current.base_qty + fill_size
            avg_price = ((current.avg_entry_price * current.base_qty) + (fill_price * fill_size)) / total_size
            position = Position(symbol, total_size, avg_price, max(current.highest_price, fill_price))
        else:
            position = Position(symbol, fill_size, fill_price, fill_price)
        self.storage.save_position(position)
        plan = stop_loss_plan(self.settings, symbol, position.avg_entry_price, position.base_qty * position.avg_entry_price)
        stop_order = self._replace_stop_loss(position, plan.stop_price)
        if not stop_order.ok:
            self._record_execution_failure(symbol, "stop_loss_failed", stop_order.error or "unknown", stop_order.raw)

    def _replace_stop_loss(self, position: Position, stop_price: float) -> StopLossOrder:
        if not self._cancel_active_stop_losses(position.symbol):
            return StopLossOrder(
                symbol=position.symbol,
                algo_id=None,
                client_order_id=OkxRestClient.client_order_id("SLFAIL", position.symbol),
                stop_price=stop_price,
                size=position.base_qty,
                ok=False,
                raw={},
                error="failed to cancel existing stop loss",
            )
        stop_order = self._place_stop_loss_for_position(position, stop_price)
        self.storage.save_stop_loss_order(stop_order)
        return stop_order

    def _cancel_active_stop_losses(self, symbol: str) -> bool:
        ok = True
        for order in self.storage.active_stop_loss_orders(symbol):
            algo_id = str(order.get("algo_id") or "")
            client_order_id = str(order.get("client_order_id") or "")
            if self.settings.trading_enabled and algo_id and algo_id != "dry-run":
                try:
                    self.exchange.cancel_stop_loss_order(symbol, algo_id)
                except Exception as exc:
                    ok = False
                    self.storage.save_bot_error("stop_loss_cancel", f"{symbol} stop cancel failed", str(exc))
                    continue
            self.storage.mark_stop_loss_inactive(client_order_id)
        return ok

    def _current_stop_price(self, position: Position) -> float:
        active = self.storage.active_stop_loss_orders(position.symbol)
        if active:
            return float(active[0].get("stop_price") or 0.0)
        return position.avg_entry_price * (1.0 - self.settings.initial_stop_loss_pct)

    def _place_stop_loss_for_position(self, position: Position, stop_price: float) -> StopLossOrder:
        if not self.settings.trading_enabled:
            return StopLossOrder(
                symbol=position.symbol,
                algo_id="dry-run",
                client_order_id=OkxRestClient.client_order_id("DRYSL", position.symbol),
                stop_price=stop_price,
                size=position.base_qty,
                ok=True,
                raw={"dry_run": True},
            )
        return self.exchange.place_stop_loss_order(position.symbol, position.base_qty, stop_price)

    def _record_execution_failure(self, symbol: str, summary: str, error: str, raw: dict) -> None:
        self.storage.save_bot_error("execution", f"{symbol} {summary}", error)
        self.storage.save_strategy_lesson(symbol, 0.0, 0.0, f"execution_failed:{summary}:{error}", str(raw))
        self.storage.save_trade_attribution(symbol, 0.0, 0.0, "执行失败", 1.0, f"{summary}: {error}", self._current_market_regime(), str(raw))
        self.storage.save_execution_event(
            symbol,
            "SPOT",
            "long",
            "",
            "execution_failure",
            "failed",
            "execution_failure",
            reason=summary,
            error=error,
            raw=json.dumps(raw, ensure_ascii=False),
        )
        self.storage.save_real_experience(
            symbol,
            "SPOT",
            "long",
            self._current_market_regime(),
            "execute",
            "failed",
            confidence=0.0,
            reason=f"{summary}: {error}",
            source="execution_failure",
            raw=json.dumps(raw, ensure_ascii=False),
        )

    def _record_trade_attribution(self, symbol: str, pnl: float, return_pct: float, summary: str, position: Position | None = None) -> None:
        market_type = position.market_type if position is not None else "SPOT"
        direction = position.direction if position is not None else "long"
        experiment_cost = abs(pnl) * 0.02 + abs(return_pct) * 0.05
        if not self.settings.ai_review_enabled:
            self.storage.save_trade_attribution(
                symbol,
                pnl,
                return_pct,
                "未知",
                0.0,
                summary,
                self._current_market_regime(),
                "",
                market_type,
                direction,
                experiment_cost,
            )
            return
        attribution = AiReviewClient(self.settings).attribute_trade(symbol, pnl, return_pct, summary, self._strategy_context())
        attribution_ok = getattr(attribution, "ok", False) is True
        attribution_reason = getattr(attribution, "reason", "") if isinstance(getattr(attribution, "reason", ""), str) else ""
        attribution_error = getattr(attribution, "error", "") if isinstance(getattr(attribution, "error", ""), str) else ""
        attribution_confidence = getattr(attribution, "confidence", 0.0)
        if not isinstance(attribution_confidence, (int, float)):
            attribution_confidence = 0.0
        self.storage.save_ai_call_audit(
            symbol=symbol,
            intent="attribution",
            ok=attribution_ok,
            action="hold",
            confidence=float(attribution_confidence or 0.0),
            prompt_chars=_int_attr(attribution, "prompt_chars"),
            response_chars=_int_attr(attribution, "response_chars"),
            duration_ms=_int_attr(attribution, "duration_ms"),
            error="" if attribution_ok else attribution_error,
            reason=attribution_reason if attribution_ok else attribution_error,
            prompt_tokens=_int_attr(attribution, "prompt_tokens"),
            completion_tokens=_int_attr(attribution, "completion_tokens"),
            total_tokens=_int_attr(attribution, "total_tokens"),
            attempted_tokens=_int_attr(attribution, "attempted_tokens"),
            retry_count=_int_attr(attribution, "retry_count"),
        )
        if attribution_ok:
            category = attribution.category if isinstance(attribution.category, str) else "未知"
            reason = attribution.reason if isinstance(attribution.reason, str) else summary
            raw_text = attribution.raw_text if isinstance(attribution.raw_text, str) else ""
            try:
                confidence = float(attribution.confidence)
            except (TypeError, ValueError):
                confidence = 0.0
            self.storage.save_trade_attribution(
                symbol,
                pnl,
                return_pct,
                category,
                confidence,
                reason,
                self._current_market_regime(),
                raw_text,
                market_type,
                direction,
                experiment_cost,
            )
        else:
            error = attribution_error or summary
            raw_text = getattr(attribution, "raw_text", "")
            if not isinstance(raw_text, str):
                raw_text = ""
            self.storage.save_trade_attribution(
                symbol,
                pnl,
                return_pct,
                "未知",
                0.0,
                str(error),
                self._current_market_regime(),
                raw_text,
                market_type,
                direction,
                experiment_cost,
            )

    def _dry_run_buy(self, candidate: CandidateScore, quote_amount: float, order_type: str = "market"):
        from okx_quant_bot.models import OrderRequest

        request = OrderRequest(
            symbol=candidate.symbol,
            side=Side.BUY,
            size=quote_amount,
            order_type=order_type,
            price=None,
            client_order_id=OkxRestClient.client_order_id("DRYB", candidate.symbol),
            reason=f"ai_buy:{candidate.reason}",
            target_currency="quote_ccy" if order_type == "market" else None,
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

    def _dry_run_limit_buy(self, candidate: CandidateScore, quote_amount: float, price: float, reason: str):
        from okx_quant_bot.models import OrderRequest

        request = OrderRequest(
            symbol=candidate.symbol,
            side=Side.BUY,
            size=quote_amount / price if price > 0 else 0.0,
            order_type="limit",
            price=price,
            client_order_id=OkxRestClient.client_order_id("DRYLB", candidate.symbol),
            reason=f"ai_limit_buy:{reason}",
        )
        result = OrderResult(True, candidate.symbol, Side.BUY, "dry-run-limit", request.client_order_id, {"dry_run": True})
        return request, result

    def _dry_run_sell(self, position: Position, reason: str, size: float | None = None):
        from okx_quant_bot.models import OrderRequest

        sell_size = position.base_qty if size is None else size
        request = OrderRequest(
            symbol=position.symbol,
            side=Side.BUY if position.direction == "short" else Side.SELL,
            size=sell_size,
            order_type="market",
            price=None,
            client_order_id=OkxRestClient.client_order_id("DRYS", position.symbol),
            reason=f"ai_sell:{reason}",
            market_type=position.market_type,
            td_mode=position.margin_mode,
            pos_side=position.direction if position.market_type == "SWAP" else None,
            reduce_only=position.market_type in {"MARGIN", "SWAP"},
            leverage=position.leverage,
            direction=position.direction,
        )
        result = OrderResult(True, position.symbol, Side.SELL, "dry-run", request.client_order_id, {"dry_run": True})
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
        review = AiReviewClient(self.settings).review_scan(scan, self.storage.open_position_count(), self._strategy_context())
        symbol = scan.best.symbol if scan.best else "MARKET"
        self.storage.save_ai_decision(symbol, "scan", "hold", 0.0, review.text if review.ok else review.error, review.text)
        self.storage.save_ai_call_audit(
            symbol=symbol,
            intent="scan",
            ok=review.ok,
            action="hold",
            confidence=0.0,
            prompt_chars=review.prompt_chars,
            response_chars=review.response_chars,
            duration_ms=review.duration_ms,
            error="" if review.ok else review.error,
            reason=review.text if review.ok else review.error,
            prompt_tokens=review.prompt_tokens,
            completion_tokens=review.completion_tokens,
            total_tokens=review.total_tokens,
            attempted_tokens=review.attempted_tokens,
            retry_count=review.retry_count,
        )
        if review.ok:
            return review.text
        return "" if review.error == "timeout" else f"AI不可用: {review.error}"

    def _review_open_trades(self, scan: MomentumScan) -> None:
        if not self.settings.trade_review_enabled:
            return
        reviews = TradeReviewEngine().mark_to_market(self.storage.open_positions(), scan.tickers, note="auto review")
        for review in reviews:
            self.storage.save_trade_review(review)
            self.storage.save_strategy_lesson(review.symbol, review.pnl_usdt, review.return_pct, review.summary, review.raw)

    def _sync_exchange_state(self, scan: MomentumScan) -> str:
        if not self.settings.trading_enabled or not hasattr(self.exchange, "get_positions"):
            return ""
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        try:
            balance_payload = self.exchange.get_balance(None)
            account = _account_snapshot_from_balance(balance_payload)
            self._save_account_snapshot(account)
            synced_positions = self._positions_from_okx(balance_payload, prices)
            for market_type in ("MARGIN", "SWAP"):
                if market_type in set(self.settings.enabled_market_types):
                    synced_positions.extend(self._derivative_positions_from_okx(market_type))
            raw_okx_count = len([position for position in synced_positions if position.is_open])
            synced_positions = _merge_duplicate_positions(synced_positions)
            before, after = self.storage.replace_open_positions(synced_positions)
            open_order_count = self._sync_open_orders()
            okx_count = len([position for position in synced_positions if position.is_open])
            self.storage.set_state("okx_last_position_count", str(okx_count))
            duplicate_count = max(0, raw_okx_count - okx_count)
            self.storage.set_state("okx_raw_position_count", str(raw_okx_count))
            self.storage.set_state("okx_merged_duplicate_position_count", str(duplicate_count))
            status = (
                f"OKX同步正常: OKX持仓={okx_count}, 本地同步前={before}, 本地同步后={after}, "
                f"open_orders={open_order_count}"
            )
            if duplicate_count > 0:
                status = f"{status}, OKX原始持仓={raw_okx_count}, 合并重复={duplicate_count}"
            self.storage.set_state("okx_sync_status", status)
            if before != after or okx_count != before:
                if self.settings.telegram_auto_reports:
                    self.notifier.send_money(f"OKX持仓 != 本地持仓，已执行同步。{status}")
                return status
            return ""
        except Exception as exc:
            message = f"OKX同步失败: {exc}"
            self.storage.set_state("okx_sync_status", message)
            self.storage.save_bot_error("okx_sync", message, traceback.format_exc())
            if self.settings.telegram_auto_reports:
                self.notifier.send_money(message)
            return message

    def _positions_from_okx(self, balance_payload: dict, prices: dict[str, float]) -> list[Position]:
        allowed_symbols = set(self.settings.symbols)
        existing = {position.symbol: position for position in self.storage.open_positions()}
        positions: list[Position] = []
        data = balance_payload.get("data", [{}])
        details = data[0].get("details", []) if data and isinstance(data[0], dict) else []
        for item in details:
            ccy = str(item.get("ccy") or "").upper()
            symbol = f"{ccy}-USDT"
            if symbol not in allowed_symbols:
                continue
            qty = _float_or_zero(item.get("eq") or item.get("cashBal") or item.get("availBal"))
            eq_usd = _float_or_zero(item.get("eqUsd") or item.get("disEq"))
            price = prices.get(symbol)
            if qty <= 0 or (eq_usd <= 1 and qty * (price or 0) <= 1):
                continue
            previous = existing.get(symbol)
            entry = previous.avg_entry_price if previous and previous.avg_entry_price > 0 else (price or 0.0)
            if entry <= 0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    base_qty=qty,
                    avg_entry_price=entry,
                    highest_price=max(entry, price or entry, previous.highest_price if previous else 0.0),
                    market_type="SPOT",
                    direction="long",
                    leverage=1.0,
                    margin_mode="cash",
                )
            )
        return positions

    def _derivative_positions_from_okx(self, market_type: str) -> list[Position]:
        payload = self.exchange.get_positions(market_type)
        positions: list[Position] = []
        for item in payload.get("data", []):
            symbol = str(item.get("instId") or "")
            if not symbol:
                continue
            size = abs(_float_or_zero(item.get("pos") or item.get("availPos")))
            if size <= 0:
                continue
            avg_price = _float_or_zero(item.get("avgPx") or item.get("openAvgPx"))
            mark_price = _float_or_zero(item.get("markPx") or item.get("last"))
            if avg_price <= 0:
                avg_price = mark_price
            if avg_price <= 0:
                continue
            pos_side = str(item.get("posSide") or "").lower()
            raw_pos = _float_or_zero(item.get("pos"))
            direction = pos_side if pos_side in {"long", "short"} else ("short" if raw_pos < 0 else "long")
            positions.append(
                Position(
                    symbol=symbol,
                    base_qty=size,
                    avg_entry_price=avg_price,
                    highest_price=max(avg_price, mark_price),
                    market_type=market_type,
                    direction=direction,
                    leverage=_float_or_zero(item.get("lever")) or self.settings.max_leverage,
                    margin_mode=str(item.get("mgnMode") or self.settings.margin_mode),
                )
            )
        return positions

    def _sync_open_orders(self) -> int:
        total = 0
        for market_type in ("SPOT", "MARGIN", "SWAP"):
            if market_type not in set(self.settings.enabled_market_types):
                continue
            try:
                payload = self.exchange.list_open_orders(inst_type=market_type)
            except TypeError:
                payload = self.exchange.list_open_orders()
            for row in payload.get("data", []):
                self.storage.save_exchange_order_snapshot(row, market_type)
                total += 1
        self.storage.set_state("okx_open_order_count", str(total))
        return total

    def _save_account_snapshot(self, account: dict[str, float]) -> None:
        for key, value in account.items():
            self.storage.set_state(f"okx_account_{key}", f"{value:.8f}")

    def _send_money_report(
        self,
        scan: MomentumScan | None = None,
        ai_note: str = "",
        note: str = "",
        force: bool = False,
    ) -> None:
        if not force and not self.settings.telegram_auto_reports:
            return
        count = int(self.storage.get_state("money_report_scan_count", "0") or "0") + 1
        self.storage.set_state("money_report_scan_count", str(count))
        if scan is not None and count % self.settings.money_report_interval_scans != 0:
            return
        try:
            snapshot = self._money_snapshot()
        except Exception as exc:
            self.storage.save_bot_error("money_report", "资金快照失败，使用缓存状态回复", str(exc))
            snapshot = self._cached_money_snapshot()
            note = f"{note}\nOKX资金快照失败，已使用缓存状态: {exc}".strip()
        sync_status = self.storage.get_state("okx_sync_status", "OKX同步未运行")
        okx_position_count = self.storage.get_state("okx_last_position_count", str(self.storage.open_position_count()))
        message = "\n".join(
            [
                f"OKX总权益: {snapshot['equity']:.2f} USDT",
                f"OKX可用: {snapshot.get('available', 0.0):.2f} USDT",
                f"OKX占用: {snapshot.get('occupied', 0.0):.2f} USDT",
                f"持仓: OKX={okx_position_count}, 本地={self.storage.open_position_count()}/{self.settings.max_open_positions}",
                f"同步: {sync_status}",
                f"买入AI: {self.storage.get_state('last_buy_ai_skip_reason', '暂无')}",
                f"今日变化: {snapshot['daily_pnl']:+.2f} USDT",
                f"本地累计盈亏: {snapshot['pnl']:+.2f} USDT",
                f"本地盈亏比: {snapshot['return_pct']:+.2f}%",
                self.storage.recent_ai_call_summary(),
                self.storage.recent_ai_call_breakdown(),
                self.storage.recent_ai_call_breakdown_since_start(),
                self.storage.execution_summary(),
                self.storage.real_experience_summary(),
            ]
        )
        if ai_note:
            message = f"{message}\nAI复盘: {ai_note}"
        if note:
            message = f"{message}\n{note}"
        self.notifier.send_money(message)
        if scan is not None:
            best = scan.best.symbol if scan.best else "NONE"
            self.storage.save_strategy_lesson(best, float(snapshot["pnl"]), float(snapshot["return_pct"]), "资金快照", message)

    def _money_snapshot(self) -> dict[str, float]:
        account = self._account_equity_snapshot()
        equity = account["equity"]
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
        return {
            "equity": equity,
            "available": account["available"],
            "occupied": account["occupied"],
            "daily_pnl": daily_pnl,
            "pnl": pnl,
            "return_pct": return_pct,
        }

    def _cached_money_snapshot(self) -> dict[str, float]:
        equity = _float_or_zero(self.storage.get_state("okx_account_equity", "10000"))
        available = _float_or_zero(self.storage.get_state("okx_account_available", str(equity)))
        occupied = _float_or_zero(self.storage.get_state("okx_account_occupied", "0"))
        baseline = _float_or_zero(self.storage.get_state("money_baseline_equity", str(equity))) or equity
        daily_key = f"money_daily_baseline:{utc_day()}"
        daily_baseline = _float_or_zero(self.storage.get_state(daily_key, str(equity))) or equity
        pnl = equity - baseline
        daily_pnl = equity - daily_baseline
        return_pct = 0.0 if baseline <= 0 else pnl / baseline * 100.0
        return {
            "equity": equity,
            "available": available,
            "occupied": occupied,
            "daily_pnl": daily_pnl,
            "pnl": pnl,
            "return_pct": return_pct,
        }

    def _account_equity(self) -> float:
        return self._account_equity_snapshot()["equity"]

    def _account_equity_snapshot(self) -> dict[str, float]:
        if not self.settings.trading_enabled:
            return {"equity": 10000.0, "available": 10000.0, "occupied": 0.0}
        payload = self.exchange.get_balance(None)
        account = _account_snapshot_from_balance(payload)
        self._save_account_snapshot(account)
        return account

    def _handle_controls(self) -> None:
        for action in self.notifier.poll_controls(self.storage):
            if action in {"stopped", "started", "reset", "status"}:
                self._send_money_report(force=True)
            elif action == "ai":
                self.notifier.send(self._ai_status_message())
            elif action == "positions":
                self.notifier.send(self._positions_message())
            elif action == "training":
                self.notifier.send(self._training_message())
            elif action == "health":
                self.notifier.send(self._health_message())
            elif action == "errors":
                self.notifier.send(self._errors_message())
            elif action == "shadow":
                self.notifier.send(self._shadow_message())
            elif action == "execution":
                self.notifier.send(self._execution_message())
            elif action == "lessons":
                self.notifier.send(self._lessons_message())
            elif action == "market":
                self.notifier.send(self._market_message())

    def _start_control_thread(self) -> None:
        if not (self.settings.telegram_bot_token and self.settings.telegram_chat_id):
            self.storage.set_state("telegram_control_thread", "disabled_missing_token_or_chat_id")
            return
        if self._controls_thread is not None and self._controls_thread.is_alive():
            return
        self._controls_stop.clear()
        self._controls_thread = threading.Thread(
            target=self._control_thread_loop,
            name="telegram-controls",
            daemon=True,
        )
        self._controls_thread.start()
        self.storage.set_state("telegram_control_thread", "running")
        print("Telegram control thread started", flush=True)

    def _control_thread_loop(self) -> None:
        while not self._controls_stop.is_set():
            self._handle_controls_safely("telegram_controls", update_runtime_stage=False)
            self._controls_stop.wait(3.0)

    def _handle_controls_safely(self, stage: str, update_runtime_stage: bool = True) -> None:
        if not self._controls_lock.acquire(blocking=False):
            return
        try:
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            if update_runtime_stage:
                self.storage.set_state("runtime_stage", stage)
                self.storage.set_state("runtime_stage_updated_at", now)
            self.storage.set_state("telegram_control_last_poll_at", now)
            self._handle_controls()
        except Exception as exc:
            self.storage.save_bot_error("telegram_controls", f"控制命令处理失败: {stage}", str(exc))
        finally:
            self._controls_lock.release()

    def _sleep_with_controls(self, seconds: int | float) -> None:
        remaining = max(0.0, float(seconds))
        self.storage.set_state("runtime_stage", "sleep")
        self.storage.set_state("runtime_stage_updated_at", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
        while remaining > 0:
            self._handle_controls_safely("sleep")
            chunk = min(5.0, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _is_paused(self) -> bool:
        return self.storage.get_state("bot_paused", "0") == "1"

    def _ensure_runtime_started(self) -> None:
        if not self.storage.get_state("runtime_started_at", ""):
            self._mark_runtime_started()

    def _mark_runtime_started(self) -> None:
        self.storage.set_state("runtime_started_at", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))

    def _save_config_snapshot(self) -> None:
        payload = {
            "okx_demo": self.settings.okx_demo,
            "trading_enabled": self.settings.trading_enabled,
            "allow_live_trading": self.settings.allow_live_trading,
            "symbols": self.settings.symbols,
            "max_open_positions": self.settings.max_open_positions,
            "scan_interval_seconds": self.settings.scan_interval_seconds,
            "openai_model": self.settings.openai_model,
            "openai_base_url": self.settings.openai_base_url,
            "openai_api_mode": self.settings.openai_api_mode,
            "ai_review_enabled": self.settings.ai_review_enabled,
            "ai_training_enabled": self.settings.ai_training_enabled,
            "ai_training_workers": self.settings.ai_training_workers,
            "ai_weekly_token_target": self.settings.ai_weekly_token_target,
            "ai_execution_decisions_enabled": self.settings.ai_execution_decisions_enabled,
            "limit_order_enabled": self.settings.limit_order_enabled,
            "replace_weak_position_enabled": self.settings.replace_weak_position_enabled,
            "risk_halt_enabled": self.settings.risk_halt_enabled,
            "momentum_exit_guard_enabled": self.settings.momentum_exit_guard_enabled,
            "momentum_take_profit_pct": self.settings.momentum_take_profit_pct,
            "momentum_stop_loss_pct": self.settings.momentum_stop_loss_pct,
            "momentum_trailing_stop_pct": self.settings.momentum_trailing_stop_pct,
            "enabled_market_types": self.settings.enabled_market_types,
            "allow_leveraged_trading": self.settings.allow_leveraged_trading,
            "allow_derivatives_trading": self.settings.allow_derivatives_trading,
            "max_leverage": self.settings.max_leverage,
            "momentum_entry_mode": self.settings.momentum_entry_mode,
            "momentum_rotation_mode": self.settings.momentum_rotation_mode,
        }
        self.storage.save_config_snapshot(json.dumps(payload, ensure_ascii=False))

    def _send_startup_diagnostics(self) -> None:
        mode = "模拟盘" if self.settings.okx_demo else "实盘"
        trading = "开启" if self.settings.trading_enabled else "关闭"
        ai = "开启" if self.settings.ai_review_enabled else "关闭"
        cadence = "每轮工作" if self.settings.ai_always_on else f"每{self.settings.ai_review_interval_scans}轮"
        warning = self.settings.ai_config_warning()
        self.notifier.send_money(
            "\n".join(
                [
                    f"启动诊断: 模式={mode}; 下单={trading}; AI={ai}; AI节奏={cadence}",
                    f"持仓上限={self.settings.max_open_positions}; 扫描间隔={self.settings.scan_interval_seconds}s",
                    f"AI候选=top {self.settings.ai_review_max_candidates}; 探索比例={self.settings.ai_exploration_fraction:.0%}",
                    f"执行决策={'开启' if self.settings.ai_execution_decisions_enabled else '关闭'}; 限价单={'开启' if self.settings.limit_order_enabled else '关闭'}",
                    f"硬退出={'开启' if self.settings.momentum_exit_guard_enabled else '关闭'}; 止盈={self.settings.momentum_take_profit_pct:.1%}; 止损={self.settings.momentum_stop_loss_pct:.1%}; 移动止盈={self.settings.momentum_trailing_stop_pct:.1%}",
                    f"风险熔断={'开启' if self.settings.risk_halt_enabled else '关闭'}; OKX技能信号源={len(self.settings.okx_skill_signal_urls)}",
                    f"AI配置提醒: {warning or '正常'}",
                ]
            )
        )

    def _ai_status_message(self) -> str:
        warning = self.settings.ai_config_warning()
        return "\n".join(
            [
                f"AI配置: 模型={self.settings.openai_model}",
                f"Base URL={self.settings.openai_base_url}",
                f"协议={self.settings.openai_api_mode}; 超时={self.settings.ai_review_timeout_seconds}s; 重试={self.settings.ai_request_retries}",
                f"训练线程={self.settings.ai_training_workers}; 周目标={self.settings.ai_weekly_token_target} token",
                f"执行决策={'开启' if self.settings.ai_execution_decisions_enabled else '关闭'}; 行情状态={'开启' if self.settings.market_regime_enabled else '关闭'}",
                f"配置自检: {warning or '正常'}",
                self.storage.recent_ai_call_summary(),
                self.storage.recent_ai_call_breakdown(),
                self.storage.recent_ai_call_breakdown_since_start(),
            ]
        )

    def _positions_message(self) -> str:
        positions = self.storage.open_positions()
        if not positions:
            return "当前没有持仓。"
        prices = self.storage.latest_market_prices()
        lines = ["当前持仓:"]
        for position in positions:
            price = prices.get(position.symbol) or prices.get(_spot_symbol(position.symbol))
            lines.append(f"- {position.symbol} route={position.market_type}/{position.direction} leverage={position.leverage:g}x")
            if price and position.avg_entry_price > 0:
                pnl = (price - position.avg_entry_price) * position.base_qty
                return_pct = (price - position.avg_entry_price) / position.avg_entry_price * 100.0
                take_profit_price = position.avg_entry_price * (1.0 + self.settings.momentum_take_profit_pct)
                stop_loss_price = position.avg_entry_price * (1.0 - self.settings.momentum_stop_loss_pct)
                to_take_profit = (take_profit_price - price) / price * 100.0
                to_stop_loss = (price - stop_loss_price) / price * 100.0
                lines.append(
                    f"- {position.symbol}: 数量{position.base_qty:.8g}, 成本{position.avg_entry_price:.8g}, "
                    f"现价{price:.8g}, 浮盈{pnl:+.2f}USDT/{return_pct:+.2f}%, "
                    f"距止盈{to_take_profit:+.2f}%, 距止损{to_stop_loss:+.2f}%"
                )
            else:
                lines.append(
                    f"- {position.symbol}: 数量{position.base_qty:.8g}, 成本{position.avg_entry_price:.8g}, 最高{position.highest_price:.8g}"
                )
        decisions = self.storage.recent_ai_decisions(limit=6)
        if decisions:
            lines.extend(["最近AI意见:", *decisions])
        return "\n".join(lines)

    def _training_message(self) -> str:
        lines = [self.storage.training_summary(current_week_key(), self.settings.ai_weekly_token_target)]
        lines.insert(0, "真实模拟盘经验训练:")
        lines.append(self.storage.recent_ai_call_breakdown())
        lines.append(self.storage.recent_ai_call_breakdown_since_start())
        lines.append(self.storage.real_experience_summary())
        if self.training_pool is not None:
            status = self.training_pool.status()
            lines.append(
                "训练池: "
                f"线程{status['alive_threads']}/{status['threads']}，队列{status['queue_size']}，"
                f"丢弃{status['dropped_tasks']}，线程异常{status['worker_errors']}"
            )
        shadows = []
        if shadows:
            lines.extend(["最近影子决策:", *shadows])
        return "\n".join(lines)

    def _health_message(self) -> str:
        warning = self.settings.ai_config_warning()
        pool_status = self.training_pool.status() if self.training_pool is not None else {}
        pending_ai = sum(1 for future in self._pending_ai.values() if not future.done())
        stage = self.storage.get_state("runtime_stage", "unknown")
        stage_at = self.storage.get_state("runtime_stage_updated_at", "")
        control_thread = self.storage.get_state("telegram_control_thread", "stopped")
        control_poll_at = self.storage.get_state("telegram_control_last_poll_at", "")
        poll_status = self.storage.get_state("telegram_poll_status", "")
        db_status = "正常"
        try:
            self.storage.open_position_count()
        except Exception as exc:
            db_status = f"异常: {exc}"
        return "\n".join(
            [
                "健康状态:",
                f"DB: {db_status}",
                f"Telegram control thread: {control_thread} {control_poll_at}",
                f"Telegram poll: {poll_status}",
                f"Telegram: {'正常' if not getattr(self.notifier, 'last_error', '') else '异常: ' + self.notifier.last_error[:120]}",
                f"AI配置: {warning or '正常'}",
                f"AI后台决策: pending={pending_ai}",
                f"当前阶段: {stage} {stage_at}",
                self.storage.recent_ai_call_breakdown_since_start(),
                (
                    "训练池: "
                    f"线程{pool_status.get('alive_threads', 0)}/{pool_status.get('threads', 0)}，"
                    f"队列{pool_status.get('queue_size', 0)}，"
                    f"丢弃{pool_status.get('dropped_tasks', 0)}"
                ),
                f"OKX模式: {'模拟盘' if self.settings.okx_demo else '实盘'}; 下单={'开启' if self.settings.trading_enabled else '关闭'}",
            ]
        )

    def _errors_message(self) -> str:
        errors = self.storage.recent_bot_errors(limit=12)
        if not errors:
            return "最近没有记录到系统异常。"
        return "\n".join(["最近异常:", *errors])

    def _shadow_message(self) -> str:
        return "影子训练已停用。AI token 现在只用于真实模拟盘持仓、候选开仓、换仓和成交归因。"
        shadows = self.storage.recent_shadow_decisions(limit=20)
        if not shadows:
            return "暂无影子全市场建议。"
        return "\n".join(["影子全市场最近建议:", *shadows])

    def _execution_message(self) -> str:
        decisions = self.storage.recent_execution_decisions(limit=12)
        guard = (
            f"硬退出: {'开启' if self.settings.momentum_exit_guard_enabled else '关闭'}; "
            f"止盈={self.settings.momentum_take_profit_pct:.1%}; "
            f"止损={self.settings.momentum_stop_loss_pct:.1%}; "
            f"移动止盈回撤={self.settings.momentum_trailing_stop_pct:.1%}"
        )
        route = (
            f"markets={','.join(self.settings.enabled_market_types)}; leverage={self.settings.max_leverage:g}x; "
            f"margin={self.settings.margin_mode}; entry={self.settings.momentum_entry_mode}; "
            f"veto={'on' if self.settings.ai_risk_veto_enabled else 'off'}; rotation={self.settings.momentum_rotation_mode}"
        )
        guard = f"{guard}\n{route}"
        if not decisions:
            return "\n".join([guard, self.storage.execution_summary(), "暂无AI执行决策。"])
        return "\n".join([guard, self.storage.execution_summary(), "最近AI执行决策:", *decisions])

    def _lessons_message(self) -> str:
        lessons = self.storage.recent_trade_attributions(limit=12)
        experience = self.storage.experience_summary()
        if not lessons:
            return "\n".join(["暂无交易归因。", experience, self.storage.real_experience_summary()])
        return "\n".join(["最近交易归因:", *lessons, experience, self.storage.real_experience_summary()])

    def _market_message(self) -> str:
        regimes = self.storage.recent_market_regimes(limit=6)
        if not regimes:
            return "暂无AI行情状态。"
        return "\n".join(["AI行情状态:", *regimes])


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


def _order_state(payload: dict) -> str:
    first = _order_payload(payload)
    state = str(first.get("state") or "").lower()
    mapping = {
        "live": "pending",
        "partially_filled": "partial",
        "partial": "partial",
        "filled": "filled",
        "canceled": "canceled",
        "cancelled": "canceled",
        "rejected": "rejected",
    }
    return mapping.get(state, "")


def _filled_size_from_order_payload(payload: dict) -> float:
    first = _order_payload(payload)
    for key in ("accFillSz", "fillSz"):
        value = first.get(key)
        if value not in {None, ""}:
            return _float_or_zero(value)
    return 0.0


def _filled_price_from_order_payload(payload: dict) -> float | None:
    first = _order_payload(payload)
    for key in ("avgPx", "fillPx"):
        value = first.get(key)
        if value not in {None, ""}:
            return _float_or_none(value)
    return None


def _order_payload(payload: dict) -> dict:
    data = payload.get("data", [{}])
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return payload if isinstance(payload, dict) else {}


def _account_snapshot_from_balance(payload: dict[str, Any]) -> dict[str, float]:
    data = payload.get("data", [{}])
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    details = first.get("details", []) if isinstance(first.get("details"), list) else []
    equity = _float_or_zero(first.get("totalEq"))
    available = 0.0
    occupied = 0.0
    for item in details:
        eq_usd = _float_or_zero(item.get("eqUsd") or item.get("disEq"))
        eq_qty = _float_or_zero(item.get("eq") or item.get("cashBal"))
        avail_usd = _float_or_zero(item.get("availEq"))
        if avail_usd <= 0 and eq_qty > 0 and eq_usd > 0:
            avail_usd = eq_usd * _float_or_zero(item.get("availBal")) / eq_qty
        frozen_usd = _float_or_zero(item.get("imr"))
        if frozen_usd <= 0 and eq_qty > 0 and eq_usd > 0:
            frozen_usd = eq_usd * _float_or_zero(item.get("frozenBal") or item.get("ordFrozen")) / eq_qty
        available += avail_usd
        occupied += frozen_usd
        if equity <= 0:
            equity += eq_usd
    if available <= 0:
        available = _float_or_zero(first.get("availEq"))
    if occupied <= 0 and equity > available:
        occupied = max(0.0, equity - available)
    return {"equity": equity, "available": available, "occupied": occupied}


def _merge_duplicate_positions(positions: list[Position]) -> list[Position]:
    merged: dict[str, Position] = {}
    for position in positions:
        if not position.is_open:
            continue
        current = merged.get(position.symbol)
        if current is None:
            merged[position.symbol] = position
            continue
        total_qty = current.base_qty + position.base_qty
        if total_qty <= 0:
            continue
        avg_entry = (
            (current.avg_entry_price * current.base_qty) + (position.avg_entry_price * position.base_qty)
        ) / total_qty
        merged[position.symbol] = Position(
            symbol=position.symbol,
            base_qty=total_qty,
            avg_entry_price=avg_entry,
            highest_price=max(current.highest_price, position.highest_price, avg_entry),
            market_type=current.market_type,
            direction=current.direction,
            leverage=max(current.leverage, position.leverage),
            margin_mode=current.margin_mode,
        )
    return list(merged.values())


def _spot_symbol(symbol: str) -> str:
    return symbol[:-5] if symbol.endswith("-SWAP") else symbol


def _float_or_zero(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value: object) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_attr(obj: object, name: str) -> int:
    value = getattr(obj, name, 0)
    return int(value) if isinstance(value, (int, float)) else 0
