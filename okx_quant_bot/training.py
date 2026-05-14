from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import date

from okx_quant_bot.ai_reviewer import AiReviewClient, _parse_trade_decision
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.momentum import MomentumScan


SHADOW_MARKETS = (
    ("spot", "现货分批买卖"),
    ("margin", "杠杆影子判断"),
    ("swap", "永续合约影子判断"),
    ("futures", "交割合约影子判断"),
    ("options", "期权方向影子判断"),
    ("grid", "网格策略影子判断"),
    ("trailing", "追踪止盈止损影子判断"),
    ("tp_sl", "止盈止损组合影子判断"),
)


@dataclass(frozen=True)
class TrainingTask:
    symbol: str
    intent: str
    prompt: str
    market_type: str = "spot"
    strategy: str = "复盘"


class AiTrainingPool:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self._queue: queue.Queue[TrainingTask] = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads or not (self.settings.ai_training_enabled and self.settings.ai_review_enabled):
            return
        for idx in range(self.settings.ai_training_workers):
            thread = threading.Thread(target=self._worker, name=f"ai-training-{idx + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def enqueue_scan(self, scan: MomentumScan, strategy_context: str) -> None:
        if not self._threads:
            return
        candidates = scan.candidates[: self.settings.ai_review_max_candidates]
        market_snapshot = _market_snapshot(scan)
        for candidate in candidates:
            self._put(
                TrainingTask(
                    symbol=candidate.symbol,
                    intent="training_candidate",
                    prompt=_candidate_training_prompt(candidate.symbol, market_snapshot, strategy_context),
                    market_type="spot",
                    strategy="候选买入复盘",
                )
            )
            for market_type, strategy in SHADOW_MARKETS:
                self._put(
                    TrainingTask(
                        symbol=candidate.symbol,
                        intent="shadow",
                        prompt=_shadow_prompt(candidate.symbol, market_type, strategy, market_snapshot, strategy_context),
                        market_type=market_type,
                        strategy=strategy,
                    )
                )

    def _put(self, task: TrainingTask) -> None:
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            self.storage.set_state("ai_training_queue_full", str(int(time.time())))

    def _worker(self) -> None:
        client = AiReviewClient(self.settings)
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                result = client.complete_training(task.prompt)
                self.storage.save_ai_call_audit(
                    symbol=task.symbol,
                    intent=task.intent,
                    ok=result.ok,
                    action=result.action if result.ok else "hold",
                    confidence=float(result.confidence),
                    prompt_chars=result.prompt_chars,
                    response_chars=result.response_chars,
                    duration_ms=result.duration_ms,
                    error="" if result.ok else result.error,
                    reason=result.reason if result.ok else result.error,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
                    retry_count=result.retry_count,
                )
                self.storage.add_training_usage(
                    week_key=current_week_key(),
                    target_tokens=self.settings.ai_weekly_token_target,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
                    ok=result.ok,
                )
                if task.intent == "shadow" and result.ok:
                    decision = _parse_trade_decision(result.raw_text)
                    self.storage.save_shadow_decision(
                        task.symbol,
                        task.market_type,
                        task.strategy,
                        decision.action,
                        decision.confidence,
                        decision.reason,
                        result.raw_text,
                    )
            finally:
                self._queue.task_done()


def current_week_key() -> str:
    year, week, _ = date.today().isocalendar()
    return f"{year}-W{week:02d}"


def _market_snapshot(scan: MomentumScan) -> str:
    lines = ["市场快照:"]
    for candidate in scan.candidates[:20]:
        lines.append(
            f"- {candidate.symbol}: 价格{candidate.price:.8g}, 24h涨幅{candidate.change_pct_24h * 100:.2f}%, "
            f"振幅{candidate.amplitude_pct_24h * 100:.2f}%, 成交额{candidate.volume_quote_24h:.2f}, "
            f"得分{candidate.total_score:.2f}, 原因{candidate.reason}"
        )
    return "\n".join(lines)


def _candidate_training_prompt(symbol: str, market_snapshot: str, strategy_context: str) -> str:
    return "\n".join(
        [
            "你正在为模拟盘交易机器人积累训练经验。",
            f"重点币种: {symbol}",
            "任务: 复盘这个币如果现在买入、等待、或卖出会分别有什么风险和机会。",
            "请只输出 JSON: {\"action\":\"buy|hold|sell\",\"confidence\":0.0,\"reason\":\"中文原因\"}",
            market_snapshot,
            "历史经验:",
            strategy_context or "- 暂无",
        ]
    )


def _shadow_prompt(symbol: str, market_type: str, strategy: str, market_snapshot: str, strategy_context: str) -> str:
    return "\n".join(
        [
            "你正在做影子全市场交易训练，不会真实下单。",
            f"币种: {symbol}",
            f"影子市场: {market_type}",
            f"影子策略: {strategy}",
            "任务: 判断如果使用这个交易手段，当前更适合 buy、hold 还是 sell。",
            "请只输出 JSON: {\"action\":\"buy|hold|sell\",\"confidence\":0.0,\"reason\":\"中文原因\"}",
            market_snapshot,
            "历史经验:",
            strategy_context or "- 暂无",
        ]
    )
