from __future__ import annotations

import time
from dataclasses import dataclass

from okx_quant_bot.ai_reviewer import AiReviewClient
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.models import CandidateScore, OrderResult, Position, Side, StopLossOrder
from okx_quant_bot.momentum import MomentumScan, run_momentum_scan, stop_loss_plan, target_position_usdt, utc_day
from okx_quant_bot.notify import Notifier
from okx_quant_bot.risk import RiskManager
from okx_quant_bot.runner import _order_failed_message, _order_recorded_message


@dataclass
class MomentumBotRunner:
    settings: Settings
    storage: Storage
    exchange: OkxRestClient
    notifier: Notifier

    def run_forever(self) -> None:
        self.settings.require_safe_trading_config()
        self.storage.init()
        if self.settings.telegram_money_only:
            self._send_money_report(note="启动")
        else:
            self.notifier.send(_momentum_start_message(self.settings.okx_demo, self.settings.trading_enabled))
            if not self.settings.binance_square_enabled:
                self.notifier.send("币安广场读取接口未启用，本轮试运行不把广场数据作为买入条件。")

        while True:
            try:
                self._handle_controls()
                if self._is_paused():
                    self._send_money_report(note="已停止开新仓")
                    time.sleep(self.settings.scan_interval_seconds)
                    continue
                self.run_once()
            except Exception as exc:
                if self.settings.telegram_money_only:
                    self._send_money_report(note=f"异常: {exc}")
                else:
                    self.notifier.send(f"信息面动量机器人异常暂停一轮：{exc}")
            time.sleep(self.settings.scan_interval_seconds)

    def run_once(self) -> MomentumScan:
        self.settings.require_safe_trading_config()
        self.storage.init()
        scan = run_momentum_scan(self.settings, self.exchange)
        self.storage.save_market_snapshots(scan.tickers)
        self.storage.save_info_signals(scan.info_signals)
        self.storage.save_candidate_scores(scan.candidates)

        if self.settings.telegram_money_only:
            ai_note = self._ai_review_text(scan)
            self._send_money_report(scan=scan, ai_note=ai_note)
        else:
            self._send_scan_summary(scan)
            self._send_daily_report(scan)
            self._send_ai_review(scan)

        candidate = self._tradable_candidate(scan)
        if candidate is None:
            return scan
        self._buy_and_protect(candidate)
        return scan

    def _tradable_candidate(self, scan: MomentumScan) -> CandidateScore | None:
        best = scan.best
        if best is None:
            if not self.settings.telegram_money_only:
                self.notifier.send("本轮没有找到符合条件的 USDT 现货候选币。")
            return None
        if not best.confirmed:
            if not self.settings.telegram_money_only:
                self.notifier.send(f"本轮最高候选 {best.symbol} 未通过确认，暂不买入。{best.reason}")
            return None
        if self.storage.open_position_count() >= self.settings.max_open_positions:
            if not self.settings.telegram_money_only:
                self.notifier.send(
                    f"已有持仓数量达到上限 {self.settings.max_open_positions}，本轮不新增仓位。"
                )
            return None
        if self.storage.get_position(best.symbol).is_open:
            if not self.settings.telegram_money_only:
                self.notifier.send(f"{best.symbol} 已有持仓，本轮不重复买入。")
            return None
        return best

    def _buy_and_protect(self, candidate: CandidateScore) -> None:
        quote_amount = target_position_usdt(self.settings)
        plan = stop_loss_plan(self.settings, candidate.symbol, candidate.price, quote_amount)
        reason = f"momentum_ai:{candidate.reason}"

        if not self.settings.trading_enabled:
            request, result = self._dry_run_buy(candidate, quote_amount)
        else:
            request, result = self.exchange.place_market_buy_quote(candidate.symbol, quote_amount, reason)
        self.storage.save_order(request, result)
        if not result.ok:
            RiskManager(self.settings, self.storage).pause_symbol(candidate.symbol, f"order_failed:{result.error}")
            if self.settings.telegram_money_only:
                self._send_money_report(note=f"下单失败: {candidate.symbol} {result.error}")
            else:
                self.notifier.send(_order_failed_message(candidate.symbol, result.error))
            return

        fill_price = _filled_price(result) or candidate.price
        fill_size = _filled_size(result) or (quote_amount / fill_price if fill_price > 0 else 0.0)
        plan = stop_loss_plan(self.settings, candidate.symbol, fill_price, fill_size * fill_price)
        self.storage.save_position(
            Position(
                symbol=candidate.symbol,
                base_qty=fill_size,
                avg_entry_price=fill_price,
                highest_price=fill_price,
            )
        )
        if not self.settings.telegram_money_only:
            self.notifier.send(_momentum_buy_message(candidate, quote_amount, fill_price, plan.stop_price))
            self.notifier.send(_order_recorded_message(candidate.symbol, Side.BUY, candidate.reason))

        stop_order = self._place_stop_loss(plan)
        self.storage.save_stop_loss_order(stop_order)
        if stop_order.ok:
            if self.settings.telegram_money_only:
                self._send_money_report(note=f"已买入 {candidate.symbol}，止损已挂")
            else:
                self.notifier.send(_stop_loss_ok_message(stop_order, plan.risk_usdt))
        else:
            RiskManager(self.settings, self.storage).pause_symbol(candidate.symbol, f"stop_loss_failed:{stop_order.error}")
            if self.settings.telegram_money_only:
                self._send_money_report(note=f"止损失败: {candidate.symbol} {stop_order.error}")
            else:
                self.notifier.send(_stop_loss_failed_message(stop_order))

    def _dry_run_buy(self, candidate: CandidateScore, quote_amount: float):
        from okx_quant_bot.models import OrderRequest

        request = OrderRequest(
            symbol=candidate.symbol,
            side=Side.BUY,
            size=quote_amount,
            order_type="market",
            price=None,
            client_order_id=OkxRestClient.client_order_id("DRYB", candidate.symbol),
            reason=f"momentum_ai:{candidate.reason}",
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

    def _send_scan_summary(self, scan: MomentumScan) -> None:
        if not scan.candidates:
            return
        top = scan.candidates[:3]
        lines = ["本轮信息面动量候选："]
        for idx, candidate in enumerate(top, start=1):
            marker = "可交易" if candidate.confirmed else "待确认"
            lines.append(
                f"{idx}. {candidate.symbol} {marker} 分数{candidate.total_score:.2f} "
                f"涨幅{candidate.change_pct_24h * 100:.2f}% 振幅{candidate.amplitude_pct_24h * 100:.2f}%"
            )
        self.notifier.send("\n".join(lines))

    def _send_daily_report(self, scan: MomentumScan) -> None:
        today = utc_day()
        state_key = f"daily_report:{today}"
        if self.storage.get_state(state_key):
            return
        best = scan.best.symbol if scan.best else "无"
        summary = (
            f"{today} 信息面动量日报：扫描{len(scan.tickers)}个交易对，"
            f"信息信号{len(scan.info_signals)}条，最高候选{best}，"
            f"当前持仓{self.storage.open_position_count()}个。"
        )
        self.storage.save_daily_report(today, summary)
        self.storage.set_state(state_key, "sent")
        self.notifier.send(summary)

    def _send_ai_review(self, scan: MomentumScan) -> None:
        text = self._ai_review_text(scan)
        if text:
            self.notifier.send("AI复盘：\n" + text)

    def _ai_review_text(self, scan: MomentumScan) -> str:
        if not self.settings.ai_review_enabled:
            return ""
        count = int(self.storage.get_state("ai_review_scan_count", "0") or "0") + 1
        self.storage.set_state("ai_review_scan_count", str(count))
        if count % self.settings.ai_review_interval_scans != 0:
            return ""
        review = AiReviewClient(self.settings).review_scan(scan, self.storage.open_position_count())
        return review.text if review.ok else f"AI不可用: {review.error}"

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
        lines = [
            f"资金状态: {snapshot['status']}",
            f"当前权益: {snapshot['equity']:.2f} USDT",
            f"累计盈亏: {snapshot['pnl']:+.2f} USDT",
            f"盈亏比: {snapshot['return_pct']:+.2f}%",
            f"持仓: {self.storage.open_position_count()}/{self.settings.max_open_positions}",
            f"模式: {'模拟盘自动下单' if self.settings.trading_enabled else '只记录不下单'}",
        ]
        if note:
            lines.append(f"备注: {note}")
        if ai_note:
            lines.append("AI资金结论: " + _one_line(ai_note))
        self.notifier.send_money("\n".join(lines))

    def _money_snapshot(self) -> dict[str, float | str]:
        equity = self._account_equity()
        baseline_raw = self.storage.get_state("money_baseline_equity", "")
        if not baseline_raw:
            self.storage.set_state("money_baseline_equity", str(equity))
            baseline = equity
        else:
            baseline = float(baseline_raw)
        pnl = equity - baseline
        return_pct = 0.0 if baseline <= 0 else pnl / baseline * 100.0
        if pnl > 0:
            status = "赚了"
        elif pnl < 0:
            status = "亏了"
        else:
            status = "持平"
        return {"equity": equity, "baseline": baseline, "pnl": pnl, "return_pct": return_pct, "status": status}

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
            if action == "stopped":
                self._send_money_report(note="按钮: 停止")
            elif action == "restarted":
                self._send_money_report(note="按钮: 重新开始")

    def _is_paused(self) -> bool:
        return self.storage.get_state("bot_paused", "0") == "1"


def _momentum_start_message(okx_demo: bool, trading_enabled: bool) -> str:
    mode = "模拟盘" if okx_demo else "实盘"
    trade = "会自动下单" if trading_enabled else "只记录模拟动作，不会发真实订单"
    return f"OKX信息面动量机器人已启动（{mode}模式，{trade}）。"


def _momentum_buy_message(
    candidate: CandidateScore,
    quote_amount: float,
    fill_price: float,
    stop_price: float,
) -> str:
    return (
        f"{candidate.symbol} 已按信息面动量买入，名义金额约{quote_amount:.2f} USDT，"
        f"成交参考价{fill_price:.8g}，保护止损价{stop_price:.8g}。"
    )


def _stop_loss_ok_message(order: StopLossOrder, risk_usdt: float) -> str:
    return (
        f"{order.symbol} 保护止损已挂好：触发价{order.stop_price:.8g}，"
        f"数量{order.size:.8g}，计划风险约{risk_usdt:.2f} USDT。"
    )


def _stop_loss_failed_message(order: StopLossOrder) -> str:
    return f"{order.symbol} 保护止损挂单失败，交易对已暂停：{order.error or '未知错误'}"


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


def _one_line(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."
