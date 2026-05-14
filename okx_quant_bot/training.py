from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import date

from okx_quant_bot.ai_reviewer import AiReviewClient
from okx_quant_bot.config import Settings
from okx_quant_bot.data import Storage
from okx_quant_bot.models import Position
from okx_quant_bot.momentum import MomentumScan


@dataclass(frozen=True)
class TrainingTask:
    symbol: str
    intent: str
    prompt: str
    market_type: str = "spot"
    direction: str = "long"
    market_regime: str = ""
    pnl_usdt: float = 0.0
    return_pct: float = 0.0
    strategy: str = "复盘"


class AiTrainingPool:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self._queue: queue.Queue[TrainingTask] = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._dropped_tasks = 0
        self._worker_errors = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._threads or not (self.settings.ai_training_enabled and self.settings.ai_review_enabled):
            return
        for idx in range(self.settings.ai_training_workers):
            thread = threading.Thread(target=self._worker, name=f"ai-training-{idx + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def status(self) -> dict[str, int]:
        with self._lock:
            dropped = self._dropped_tasks
            errors = self._worker_errors
        alive = sum(1 for thread in self._threads if thread.is_alive())
        return {
            "configured_workers": int(self.settings.ai_training_workers),
            "threads": len(self._threads),
            "alive_threads": alive,
            "queue_size": self._queue.qsize(),
            "dropped_tasks": dropped,
            "worker_errors": errors,
        }

    def enqueue_scan(
        self,
        scan: MomentumScan,
        strategy_context: str,
        positions: list[Position] | tuple[Position, ...] = (),
    ) -> None:
        if not self._threads:
            return
        market_snapshot = _market_snapshot(scan)
        prices = {ticker.symbol: ticker.last for ticker in scan.tickers}
        for position in positions:
            current_price = prices.get(position.symbol) or prices.get(_spot_symbol(position.symbol), position.avg_entry_price)
            self._put(
                TrainingTask(
                    symbol=position.symbol,
                    intent="portfolio_training",
                    prompt=_position_training_prompt(position, current_price, market_snapshot, strategy_context),
                    market_type=position.market_type.lower(),
                    direction=position.direction,
                    pnl_usdt=position.pnl(current_price),
                    return_pct=position.return_pct(current_price),
                    strategy="真实模拟盘持仓复盘",
                )
            )

    def _put(self, task: TrainingTask) -> None:
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            with self._lock:
                self._dropped_tasks += 1
            self.storage.set_state("ai_training_queue_full", str(int(time.time())))

    def _worker(self) -> None:
        client = AiReviewClient(self.settings)
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle_task(client, task)
            except Exception as exc:
                with self._lock:
                    self._worker_errors += 1
                self.storage.save_bot_error("ai_training_worker", "训练线程异常，已继续运行", str(exc))
            finally:
                self._queue.task_done()

    def _handle_task(self, client: AiReviewClient, task: TrainingTask) -> None:
        result = client.complete_training(task.prompt)
        experience_saved = False
        if result.ok:
            self.storage.save_real_experience(
                symbol=task.symbol,
                market_type=task.market_type.upper(),
                direction=task.direction,
                market_regime=task.market_regime,
                action=result.action,
                result="training_signal",
                pnl_usdt=task.pnl_usdt,
                return_pct=task.return_pct,
                confidence=float(result.confidence),
                reason=result.reason,
                source=task.intent,
                raw=result.raw_text,
            )
            experience_saved = True
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
            attempted_tokens=result.attempted_tokens,
            retry_count=result.retry_count,
        )
        self.storage.add_training_usage(
            week_key=current_week_key(),
            target_tokens=self.settings.ai_weekly_token_target,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            attempted_tokens=result.attempted_tokens,
            ok=result.ok,
            experience_saved=experience_saved,
        )


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


def _position_training_prompt(
    position: Position,
    current_price: float,
    market_snapshot: str,
    strategy_context: str,
) -> str:
    return "\n".join(
        [
            "你正在为OKX模拟盘真实持仓做经验复盘，不要做影子市场假设。",
            f"持仓: {position.symbol}",
            f"市场: {position.market_type}/{position.direction}",
            f"数量: {position.base_qty:.8g}",
            f"成本: {position.avg_entry_price:.8g}",
            f"现价: {current_price:.8g}",
            f"浮动收益率: {position.return_pct(current_price):+.2f}%",
            '请只输出 JSON: {"action":"hold|sell","confidence":0.0,"reason":"中文原因"}',
            market_snapshot,
            "历史经验:",
            strategy_context or "- 暂无",
        ]
    )


def _spot_symbol(symbol: str) -> str:
    return symbol[:-5] if symbol.endswith("-SWAP") else symbol
