from __future__ import annotations

import time
import traceback
import json
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace

from okx_quant_bot.ai_reviewer import AiReviewClient
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.models import CandidateScore, OrderResult, Position, Side, StopLossOrder
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

    def run_forever(self) -> None:
        self.settings.require_safe_trading_config()
        self.storage.init()
        self._save_config_snapshot()
        self.notifier.setup_commands()
        if self.training_pool is None:
            self.training_pool = AiTrainingPool(self.settings, self.storage)
        self.training_pool.start()
        self._send_startup_diagnostics()
        self._send_money_report(force=True)

        while True:
            try:
                self._handle_controls()
                if not self._is_paused():
                    self.run_once()
            except Exception as exc:
                self.storage.save_bot_error("main_loop", "主循环异常，机器人会继续运行并等待下一轮。", traceback.format_exc())
                self.notifier.send(f"主循环异常，机器人会继续运行并等待下一轮: {exc}")
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

        self._collect_finished_ai_tasks()
        ai_note = self._latest_ai_review_text(scan)
        self._sell_positions_with_recent_ai(scan)
        self._submit_ai_review(scan)
        self._submit_sell_reviews(scan)
        self._collect_finished_ai_tasks(grace_seconds=0.05)
        self._sell_positions_with_recent_ai(scan)
        for candidate in self._ai_learning_candidates(scan):
            self._submit_buy_review(scan, candidate)
            self._collect_finished_ai_tasks(grace_seconds=0.05)
            if self._latest_ai_allows(candidate.symbol, "buy") and self._is_tradable_candidate(candidate):
                self._buy_and_protect(candidate)

        if self.training_pool is not None:
            self.training_pool.enqueue_scan(scan, self._strategy_context())
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

    def _submit_buy_review(self, scan: MomentumScan, candidate: CandidateScore) -> None:
        self._submit_ai_task(f"buy:{candidate.symbol}", self._ai_buy_decision, scan, candidate)

    def _submit_sell_reviews(self, scan: MomentumScan) -> None:
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in self.storage.open_positions():
            price = prices.get(position.symbol)
            if price is None:
                continue
            self._submit_ai_task(f"sell:{position.symbol}", self._ai_sell_decision, scan, position, price)

    def _latest_ai_allows(self, symbol: str, intent: str) -> bool:
        decision = self.storage.latest_ai_decision(symbol, intent, self._ai_decision_ttl_seconds())
        if not decision:
            return False
        action = str(decision.get("action") or "")
        confidence = float(decision.get("confidence") or 0.0)
        return action == intent and confidence >= 0.65

    def _ai_decision_ttl_seconds(self) -> int:
        return max(60, int(self.settings.scan_interval_seconds * 3))

    def _latest_ai_review_text(self, scan: MomentumScan) -> str:
        symbol = scan.best.symbol if scan.best else "MARKET"
        decision = self.storage.latest_ai_decision(symbol, "scan", self._ai_decision_ttl_seconds())
        if not decision:
            return ""
        return str(decision.get("reason") or "")[:500]

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
            prompt_tokens=int(getattr(decision, "prompt_tokens", 0)),
            completion_tokens=int(getattr(decision, "completion_tokens", 0)),
            total_tokens=int(getattr(decision, "total_tokens", 0)),
            attempted_tokens=int(getattr(decision, "attempted_tokens", 0)),
            retry_count=int(getattr(decision, "retry_count", 0)),
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
            self.storage.save_bot_error("order_buy", f"{candidate.symbol} 买入失败", result.error or "unknown")
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
            self.storage.save_bot_error("stop_loss", f"{candidate.symbol} 止损单失败", stop_order.error or "unknown")
            self.storage.save_strategy_lesson(
                candidate.symbol,
                0.0,
                0.0,
                f"stop_loss_failed:{stop_order.error or 'unknown'}",
                str(stop_order.raw),
            )

    def _sell_positions_with_recent_ai(self, scan: MomentumScan) -> None:
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in self.storage.open_positions():
            price = prices.get(position.symbol)
            if price is None:
                continue
            decision = self.storage.latest_ai_decision(position.symbol, "sell", self._ai_decision_ttl_seconds())
            if not decision:
                continue
            if str(decision.get("action")) == "sell" and float(decision.get("confidence") or 0.0) >= 0.65:
                self._sell_position(position, price, str(decision.get("reason") or "AI建议卖出"))

    def _sell_positions_with_ai(self, scan: MomentumScan) -> None:
        self._sell_positions_with_recent_ai(scan)

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
            self.storage.save_bot_error("order_sell", f"{position.symbol} 卖出失败", result.error or "unknown")
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
        review = AiReviewClient(self.settings).review_scan(
            scan,
            self.storage.open_position_count(),
            self._strategy_context(),
        )
        symbol = scan.best.symbol if scan.best else "MARKET"
        self.storage.save_ai_decision(
            symbol,
            "scan",
            "hold",
            0.0,
            review.text if review.ok else review.error,
            review.text,
        )
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
        force: bool = False,
    ) -> None:
        if not force and not self.settings.telegram_auto_reports:
            return
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

    def _is_paused(self) -> bool:
        return self.storage.get_state("bot_paused", "0") == "1"

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
            "risk_halt_enabled": self.settings.risk_halt_enabled,
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
                f"配置自检: {warning or '正常'}",
                self.storage.recent_ai_call_summary(),
            ]
        )

    def _positions_message(self) -> str:
        positions = self.storage.open_positions()
        if not positions:
            return "当前没有持仓。"
        lines = ["当前持仓:"]
        for position in positions:
            lines.append(
                f"- {position.symbol}: 数量{position.base_qty:.8g}, 成本{position.avg_entry_price:.8g}, 最高{position.highest_price:.8g}"
            )
        decisions = self.storage.recent_ai_decisions(limit=6)
        if decisions:
            lines.extend(["最近AI意见:", *decisions])
        return "\n".join(lines)

    def _training_message(self) -> str:
        lines = [self.storage.training_summary(current_week_key(), self.settings.ai_weekly_token_target)]
        if self.training_pool is not None:
            status = self.training_pool.status()
            lines.append(
                "训练池: "
                f"线程{status['alive_threads']}/{status['threads']}，队列{status['queue_size']}，"
                f"丢弃{status['dropped_tasks']}，线程异常{status['worker_errors']}"
            )
        shadows = self.storage.recent_shadow_decisions(limit=6)
        if shadows:
            lines.extend(["最近影子决策:", *shadows])
        return "\n".join(lines)

    def _health_message(self) -> str:
        warning = self.settings.ai_config_warning()
        pool_status = self.training_pool.status() if self.training_pool is not None else {}
        pending_ai = sum(1 for future in self._pending_ai.values() if not future.done())
        db_status = "正常"
        try:
            self.storage.open_position_count()
        except Exception as exc:
            db_status = f"异常: {exc}"
        return "\n".join(
            [
                "健康状态:",
                f"DB: {db_status}",
                f"Telegram: {'正常' if not getattr(self.notifier, 'last_error', '') else '异常: ' + self.notifier.last_error[:120]}",
                f"AI配置: {warning or '正常'}",
                f"AI后台决策: pending={pending_ai}",
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
        shadows = self.storage.recent_shadow_decisions(limit=20)
        if not shadows:
            return "暂无影子全市场建议。"
        return "\n".join(["影子全市场最近建议:", *shadows])


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
