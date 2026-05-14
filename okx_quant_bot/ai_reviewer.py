from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from okx_quant_bot.config import Settings
from okx_quant_bot.models import CandidateScore, InfoSignal, Position
from okx_quant_bot.momentum import MomentumScan


ENTRY_MODES = {"market_now", "limit_pullback", "split_limit", "breakout_confirm", "wait"}
EXIT_MODES = {"hold", "sell_all", "sell_partial", "trail_profit", "move_to_breakeven"}
SIZE_MODES = {"explore", "normal", "strong", "reduced"}
STOP_MODES = {"fixed", "wide", "tight", "breakeven", "trailing"}
REPLACE_MODES = {"none", "replace_weakest", "free_cash_only"}
ATTRIBUTION_CATEGORIES = {
    "追高",
    "假突破",
    "流动性不足",
    "新闻误判",
    "止损太紧",
    "止盈太早",
    "入场太晚",
    "执行失败",
    "未知",
}
MARKET_REGIMES = {"单边上涨", "震荡", "急跌反弹", "高波动插针", "主流吸血", "山寨轮动"}


@dataclass(frozen=True)
class AiReview:
    ok: bool
    text: str
    error: str = ""
    prompt_chars: int = 0
    response_chars: int = 0
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempted_tokens: int = 0
    retry_count: int = 0


@dataclass(frozen=True)
class AiTradeDecision:
    ok: bool
    action: str = "hold"
    confidence: float = 0.0
    reason: str = ""
    raw_text: str = ""
    error: str = ""
    entry_mode: str = "wait"
    exit_mode: str = "hold"
    size_mode: str = "normal"
    stop_mode: str = "fixed"
    replace_mode: str = "none"
    prompt_chars: int = 0
    response_chars: int = 0
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempted_tokens: int = 0
    retry_count: int = 0

    @property
    def approved_buy(self) -> bool:
        return self.ok and self.action == "buy" and self.confidence >= 0.65

    @property
    def approved_sell(self) -> bool:
        return self.ok and self.action == "sell" and self.confidence >= 0.65


@dataclass(frozen=True)
class AiMarketRegime:
    ok: bool
    regime: str = "震荡"
    confidence: float = 0.0
    reason: str = ""
    raw_text: str = ""
    error: str = ""
    prompt_chars: int = 0
    response_chars: int = 0
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempted_tokens: int = 0
    retry_count: int = 0


@dataclass(frozen=True)
class AiTradeAttribution:
    ok: bool
    category: str = "未知"
    confidence: float = 0.0
    reason: str = ""
    raw_text: str = ""
    error: str = ""
    prompt_chars: int = 0
    response_chars: int = 0
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempted_tokens: int = 0
    retry_count: int = 0


class AiReviewClient:
    def __init__(
        self,
        settings: Settings,
        opener: Callable[[urllib.request.Request, float], bytes] | None = None,
    ) -> None:
        self.settings = settings
        self._opener = opener or _urlopen_bytes

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ai_review_enabled and self.settings.openai_api_key)

    def review_scan(
        self,
        scan: MomentumScan,
        open_position_count: int,
        strategy_memory: str = "",
    ) -> AiReview:
        if not self.enabled:
            return AiReview(False, "", "AI未启用或缺少 OPENAI_API_KEY/MIMO_API_KEY。")
        prompt = _scan_prompt(self.settings, scan, open_position_count, strategy_memory)
        result = self._complete(prompt)
        review_ok, review_text, review_error, repaired = self._parse_scan_review_with_repair(result) if result.ok else (False, "", result.error, None)
        return AiReview(
            ok=review_ok,
            text=_telegram_sized(review_text) if review_ok else "",
            error="" if review_ok else review_error,
            prompt_chars=result.prompt_chars + int(getattr(repaired, "prompt_chars", 0) if repaired else 0),
            response_chars=result.response_chars + int(getattr(repaired, "response_chars", 0) if repaired else 0),
            duration_ms=result.duration_ms + int(getattr(repaired, "duration_ms", 0) if repaired else 0),
            prompt_tokens=result.prompt_tokens + int(getattr(repaired, "prompt_tokens", 0) if repaired else 0),
            completion_tokens=result.completion_tokens + int(getattr(repaired, "completion_tokens", 0) if repaired else 0),
            total_tokens=result.total_tokens + int(getattr(repaired, "total_tokens", 0) if repaired else 0),
            attempted_tokens=result.attempted_tokens + int(getattr(repaired, "attempted_tokens", 0) if repaired else 0),
            retry_count=result.retry_count + int(getattr(repaired, "retry_count", 0) if repaired else 0),
        )

    def decide_buy(
        self,
        scan: MomentumScan,
        candidate: CandidateScore,
        open_position_count: int,
        strategy_memory: str = "",
        market_regime: str = "",
    ) -> AiTradeDecision:
        prompt = _buy_prompt(self.settings, scan, candidate, open_position_count, strategy_memory, market_regime)
        return self._decide(prompt, "buy")

    def decide_sell(
        self,
        scan: MomentumScan,
        position: Position,
        current_price: float,
        strategy_memory: str = "",
        market_regime: str = "",
    ) -> AiTradeDecision:
        prompt = _sell_prompt(self.settings, scan, position, current_price, strategy_memory, market_regime)
        return self._decide(prompt, "sell")

    def decide_market_regime(self, scan: MomentumScan, strategy_memory: str = "") -> AiMarketRegime:
        result = self._complete(_market_regime_prompt(scan, strategy_memory))
        if not result.ok:
            return AiMarketRegime(
                False,
                error=result.error,
                prompt_chars=result.prompt_chars,
                response_chars=result.response_chars,
                duration_ms=result.duration_ms,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                attempted_tokens=result.attempted_tokens,
                retry_count=result.retry_count,
            )
        return self._parse_market_regime_with_repair(result)

    def attribute_trade(
        self,
        symbol: str,
        pnl_usdt: float,
        return_pct: float,
        summary: str,
        strategy_memory: str = "",
    ) -> AiTradeAttribution:
        result = self._complete(_attribution_prompt(symbol, pnl_usdt, return_pct, summary, strategy_memory))
        if not result.ok:
            return AiTradeAttribution(
                False,
                error=result.error,
                prompt_chars=result.prompt_chars,
                response_chars=result.response_chars,
                duration_ms=result.duration_ms,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                attempted_tokens=result.attempted_tokens,
                retry_count=result.retry_count,
            )
        return self._parse_trade_attribution_with_repair(result)

    def complete_training(self, prompt: str) -> AiTradeDecision:
        return self._decide(prompt, "training")

    def _decide(self, prompt: str, parse_source: str) -> AiTradeDecision:
        result = self._complete(prompt)
        if not result.ok:
            return result
        parsed = _parse_trade_decision(result.raw_text)
        if not parsed.ok:
            repaired = self._complete(_json_repair_prompt(result.raw_text, _trade_decision_schema()))
            if repaired.ok:
                repaired_parsed = _parse_trade_decision(repaired.raw_text)
                if repaired_parsed.ok:
                    return AiTradeDecision(
                        ok=True,
                        action=repaired_parsed.action,
                        confidence=repaired_parsed.confidence,
                        reason=repaired_parsed.reason,
                        raw_text=repaired.raw_text,
                        entry_mode=repaired_parsed.entry_mode,
                        exit_mode=repaired_parsed.exit_mode,
                        size_mode=repaired_parsed.size_mode,
                        stop_mode=repaired_parsed.stop_mode,
                        replace_mode=repaired_parsed.replace_mode,
                        prompt_chars=result.prompt_chars + repaired.prompt_chars,
                        response_chars=result.response_chars + repaired.response_chars,
                        duration_ms=result.duration_ms + repaired.duration_ms,
                        prompt_tokens=result.prompt_tokens + repaired.prompt_tokens,
                        completion_tokens=result.completion_tokens + repaired.completion_tokens,
                        total_tokens=result.total_tokens + repaired.total_tokens,
                        attempted_tokens=result.attempted_tokens + repaired.attempted_tokens,
                        retry_count=result.retry_count + repaired.retry_count,
                    )
            error = _parse_failed_error(parse_source, parsed.error)
            return AiTradeDecision(
                False,
                raw_text=result.raw_text,
                error=error,
                prompt_chars=result.prompt_chars + getattr(repaired, "prompt_chars", 0),
                response_chars=result.response_chars + getattr(repaired, "response_chars", 0),
                duration_ms=result.duration_ms + getattr(repaired, "duration_ms", 0),
                prompt_tokens=result.prompt_tokens + getattr(repaired, "prompt_tokens", 0),
                completion_tokens=result.completion_tokens + getattr(repaired, "completion_tokens", 0),
                total_tokens=result.total_tokens + getattr(repaired, "total_tokens", 0),
                attempted_tokens=result.attempted_tokens + getattr(repaired, "attempted_tokens", 0),
                retry_count=result.retry_count + getattr(repaired, "retry_count", 0),
            )
        return AiTradeDecision(
            ok=parsed.ok,
            action=parsed.action,
            confidence=parsed.confidence,
            reason=parsed.reason,
            raw_text=parsed.raw_text,
            error=parsed.error,
            entry_mode=parsed.entry_mode,
            exit_mode=parsed.exit_mode,
            size_mode=parsed.size_mode,
            stop_mode=parsed.stop_mode,
            replace_mode=parsed.replace_mode,
            prompt_chars=result.prompt_chars,
            response_chars=result.response_chars,
            duration_ms=result.duration_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            attempted_tokens=result.attempted_tokens,
            retry_count=result.retry_count,
        )

    def _parse_market_regime_with_repair(self, result: AiTradeDecision) -> AiMarketRegime:
        parsed = _parse_market_regime(result.raw_text)
        if parsed.ok:
            return AiMarketRegime(
                True,
                regime=parsed.regime,
                confidence=parsed.confidence,
                reason=parsed.reason,
                raw_text=parsed.raw_text,
                prompt_chars=result.prompt_chars,
                response_chars=result.response_chars,
                duration_ms=result.duration_ms,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                attempted_tokens=result.attempted_tokens,
                retry_count=result.retry_count,
            )
        repaired = self._complete(_json_repair_prompt(result.raw_text, _market_regime_schema()))
        if repaired.ok:
            repaired_parsed = _parse_market_regime(repaired.raw_text)
            if repaired_parsed.ok:
                return AiMarketRegime(
                    True,
                    regime=repaired_parsed.regime,
                    confidence=repaired_parsed.confidence,
                    reason=repaired_parsed.reason,
                    raw_text=repaired.raw_text,
                    prompt_chars=result.prompt_chars + repaired.prompt_chars,
                    response_chars=result.response_chars + repaired.response_chars,
                    duration_ms=result.duration_ms + repaired.duration_ms,
                    prompt_tokens=result.prompt_tokens + repaired.prompt_tokens,
                    completion_tokens=result.completion_tokens + repaired.completion_tokens,
                    total_tokens=result.total_tokens + repaired.total_tokens,
                    attempted_tokens=result.attempted_tokens + repaired.attempted_tokens,
                    retry_count=result.retry_count + repaired.retry_count,
                )
        return AiMarketRegime(
            False,
            raw_text=result.raw_text,
            error=_parse_failed_error("market_regime", parsed.error),
            prompt_chars=result.prompt_chars + getattr(repaired, "prompt_chars", 0),
            response_chars=result.response_chars + getattr(repaired, "response_chars", 0),
            duration_ms=result.duration_ms + getattr(repaired, "duration_ms", 0),
            prompt_tokens=result.prompt_tokens + getattr(repaired, "prompt_tokens", 0),
            completion_tokens=result.completion_tokens + getattr(repaired, "completion_tokens", 0),
            total_tokens=result.total_tokens + getattr(repaired, "total_tokens", 0),
            attempted_tokens=result.attempted_tokens + getattr(repaired, "attempted_tokens", 0),
            retry_count=result.retry_count + getattr(repaired, "retry_count", 0),
        )

    def _parse_trade_attribution_with_repair(self, result: AiTradeDecision) -> AiTradeAttribution:
        parsed = _parse_trade_attribution(result.raw_text)
        if parsed.ok:
            return AiTradeAttribution(
                True,
                category=parsed.category,
                confidence=parsed.confidence,
                reason=parsed.reason,
                raw_text=parsed.raw_text,
                prompt_chars=result.prompt_chars,
                response_chars=result.response_chars,
                duration_ms=result.duration_ms,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                attempted_tokens=result.attempted_tokens,
                retry_count=result.retry_count,
            )
        repaired = self._complete(_json_repair_prompt(result.raw_text, _attribution_schema()))
        if repaired.ok:
            repaired_parsed = _parse_trade_attribution(repaired.raw_text)
            if repaired_parsed.ok:
                return AiTradeAttribution(
                    True,
                    category=repaired_parsed.category,
                    confidence=repaired_parsed.confidence,
                    reason=repaired_parsed.reason,
                    raw_text=repaired.raw_text,
                    prompt_chars=result.prompt_chars + repaired.prompt_chars,
                    response_chars=result.response_chars + repaired.response_chars,
                    duration_ms=result.duration_ms + repaired.duration_ms,
                    prompt_tokens=result.prompt_tokens + repaired.prompt_tokens,
                    completion_tokens=result.completion_tokens + repaired.completion_tokens,
                    total_tokens=result.total_tokens + repaired.total_tokens,
                    attempted_tokens=result.attempted_tokens + repaired.attempted_tokens,
                    retry_count=result.retry_count + repaired.retry_count,
                )
        return AiTradeAttribution(
            False,
            raw_text=result.raw_text,
            error=_parse_failed_error("attribution", parsed.error),
            prompt_chars=result.prompt_chars + getattr(repaired, "prompt_chars", 0),
            response_chars=result.response_chars + getattr(repaired, "response_chars", 0),
            duration_ms=result.duration_ms + getattr(repaired, "duration_ms", 0),
            prompt_tokens=result.prompt_tokens + getattr(repaired, "prompt_tokens", 0),
            completion_tokens=result.completion_tokens + getattr(repaired, "completion_tokens", 0),
            total_tokens=result.total_tokens + getattr(repaired, "total_tokens", 0),
            attempted_tokens=result.attempted_tokens + getattr(repaired, "attempted_tokens", 0),
            retry_count=result.retry_count + getattr(repaired, "retry_count", 0),
        )

    def _parse_scan_review_with_repair(self, result: AiTradeDecision) -> tuple[bool, str, str, AiTradeDecision | None]:
        parsed = _parse_scan_review(result.raw_text)
        if parsed is not None:
            return True, parsed, "", None
        repaired = self._complete(_json_repair_prompt(result.raw_text, _scan_review_schema()))
        if repaired.ok:
            repaired_parsed = _parse_scan_review(repaired.raw_text)
            if repaired_parsed is not None:
                return True, repaired_parsed, "", repaired
        return False, "", _parse_failed_error("scan", "AI 扫描复盘 JSON 解析失败。"), repaired

    def _complete(self, prompt: str) -> AiTradeDecision:
        if not self.enabled:
            return AiTradeDecision(
                False,
                error="AI未启用或缺少 OPENAI_API_KEY/MIMO_API_KEY。",
                prompt_chars=len(prompt),
                attempted_tokens=_estimate_tokens(prompt),
            )
        body = self._request_body(prompt)
        request = urllib.request.Request(
            self._request_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        started = time.monotonic()
        last_error = ""
        attempts = self.settings.ai_request_retries + 1
        for attempt in range(attempts):
            try:
                raw = self._opener(request, self.settings.ai_review_timeout_seconds)
                payload = json.loads(raw.decode("utf-8"))
                text = _extract_ai_text(payload).strip()
                usage = _extract_usage(payload)
                if not text:
                    raise ValueError("AI 响应没有正文。")
                return AiTradeDecision(
                    True,
                    raw_text=text,
                    prompt_chars=len(prompt),
                    response_chars=len(text),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    attempted_tokens=usage["total_tokens"] or _estimate_tokens(prompt),
                    retry_count=attempt,
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code < 500 and exc.code not in {408, 409, 429}:
                    break
            except TimeoutError:
                last_error = "timeout"
            except Exception as exc:
                last_error = "timeout" if "timed out" in str(exc).lower() else f"请求失败: {exc}"
            if attempt < attempts - 1 and self.settings.ai_retry_backoff_seconds > 0:
                time.sleep(self.settings.ai_retry_backoff_seconds * (attempt + 1))

        return AiTradeDecision(
            False,
            error=last_error,
            prompt_chars=len(prompt),
            duration_ms=int((time.monotonic() - started) * 1000),
            attempted_tokens=_estimate_tokens(prompt) * attempts,
            retry_count=max(0, attempts - 1),
        )

    def _request_url(self) -> str:
        if self.settings.openai_api_mode == "anthropic":
            path = "v1/messages"
        else:
            path = "chat/completions" if self.settings.openai_api_mode == "chat" else "responses"
        return f"{self.settings.openai_base_url}/{path}"

    def _request_body(self, prompt: str) -> dict:
        if self.settings.openai_api_mode == "anthropic":
            return {
                "model": self.settings.openai_model,
                "system": _instructions(),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.settings.ai_review_max_tokens,
            }
        if self.settings.openai_api_mode == "chat":
            body = {
                "model": self.settings.openai_model,
                "messages": [
                    {"role": "system", "content": _instructions()},
                    {"role": "user", "content": prompt},
                ],
                "max_completion_tokens": self.settings.ai_review_max_tokens,
                "temperature": 1.0,
                "top_p": 0.95,
                "stream": False,
                "stop": None,
                "frequency_penalty": 0,
                "presence_penalty": 0,
            }
            if "mimo" in self.settings.openai_model.lower() or "xiaomimimo" in self.settings.openai_base_url.lower():
                body["thinking"] = {"type": "disabled"}
            return body
        return {
            "model": self.settings.openai_model,
            "instructions": _instructions(),
            "input": prompt,
            "max_output_tokens": self.settings.ai_review_max_tokens,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "okx-quant-bot/0.1"}
        if self.settings.openai_api_mode == "anthropic":
            headers["x-api-key"] = self.settings.openai_api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {self.settings.openai_api_key}"
        return headers


def _instructions() -> str:
    return (
        "你是加密货币模拟盘交易训练助手。交易和执行决策必须只输出 JSON，不要 Markdown。"
        "原因必须使用中文，不要承诺一定盈利，不要建议移除止损。"
    )


def _scan_prompt(settings: Settings, scan: MomentumScan, open_position_count: int, strategy_memory: str = "") -> str:
    candidates = scan.candidates[: settings.ai_review_max_candidates]
    lines = [
        "复盘本轮 OKX 现货动量扫描，只输出一句中文资金结论。",
        f"模式: {'模拟盘' if settings.okx_demo else '实盘'}; 当前持仓: {open_position_count}/{settings.max_open_positions}",
        f"单笔目标名义: {settings.target_position_usdt:.2f} USDT; 单笔风险预算: {settings.risk_per_trade_usdt:.2f} USDT",
        "策略记忆:",
        strategy_memory or "- 暂无",
        "候选:",
    ]
    info_by_symbol = _group_info(scan.info_signals)
    for candidate in candidates:
        lines.extend(_candidate_lines(candidate, info_by_symbol.get(candidate.symbol, [])))
    return "\n".join(lines)


def _buy_prompt(
    settings: Settings,
    scan: MomentumScan,
    candidate: CandidateScore,
    open_position_count: int,
    strategy_memory: str = "",
    market_regime: str = "",
) -> str:
    info_by_symbol = _group_info(scan.info_signals)
    lines = [
        "判断是否允许买入这个候选币，并决定具体执行方式。只输出 JSON。",
        'JSON格式: {"action":"buy|hold","entry_mode":"market_now|limit_pullback|split_limit|breakout_confirm|wait","size_mode":"explore|normal|strong|reduced","stop_mode":"fixed|wide|tight|breakeven|trailing","replace_mode":"none|replace_weakest|free_cash_only","confidence":0.0,"reason":"中文原因"}',
        f"行情状态: {market_regime or '未知'}",
        f"候选: {candidate.symbol}",
        f"价格: {candidate.price:.8g}",
        f"24h涨幅: {candidate.change_pct_24h * 100:.2f}%",
        f"24h振幅: {candidate.amplitude_pct_24h * 100:.2f}%",
        f"成交额: {candidate.volume_quote_24h:.2f}",
        f"规则得分: {candidate.total_score:.2f}",
        f"规则原因: {candidate.reason}",
        f"当前持仓: {open_position_count}/{settings.max_open_positions}",
        f"单笔目标: {settings.target_position_usdt:.2f} USDT; 单笔风险: {settings.risk_per_trade_usdt:.2f} USDT",
        "相关新闻和公开情报:",
    ]
    lines.extend(_signal_lines(info_by_symbol.get(candidate.symbol, []), limit=settings.intelligence_max_items))
    lines.extend(["策略经验:", strategy_memory or "- 暂无"])
    return "\n".join(lines)


def _sell_prompt(
    settings: Settings,
    scan: MomentumScan,
    position: Position,
    current_price: float,
    strategy_memory: str = "",
    market_regime: str = "",
) -> str:
    pnl = (current_price - position.avg_entry_price) * position.base_qty
    return_pct = 0.0 if position.avg_entry_price <= 0 else (current_price - position.avg_entry_price) / position.avg_entry_price * 100
    info_by_symbol = _group_info(scan.info_signals)
    lines = [
        "判断当前持仓是否应该卖出或调整止盈止损。只输出 JSON。",
        'JSON格式: {"action":"hold|sell","exit_mode":"hold|sell_all|sell_partial|trail_profit|move_to_breakeven","confidence":0.0,"reason":"中文原因"}',
        f"行情状态: {market_regime or '未知'}",
        f"持仓: {position.symbol}",
        f"成本: {position.avg_entry_price:.8g}",
        f"现价: {current_price:.8g}",
        f"数量: {position.base_qty:.8g}",
        f"浮动盈亏: {pnl:+.2f} USDT",
        f"收益率: {return_pct:+.2f}%",
        "相关新闻和公开情报:",
    ]
    lines.extend(_signal_lines(info_by_symbol.get(position.symbol, []), limit=settings.intelligence_max_items))
    lines.extend(["策略经验:", strategy_memory or "- 暂无"])
    return "\n".join(lines)


def _market_regime_prompt(scan: MomentumScan, strategy_memory: str = "") -> str:
    lines = [
        "判断当前加密市场状态。只输出 JSON。",
        'JSON格式: {"regime":"单边上涨|震荡|急跌反弹|高波动插针|主流吸血|山寨轮动","confidence":0.0,"reason":"中文原因"}',
        "候选和行情:",
    ]
    for candidate in scan.candidates[:20]:
        lines.append(
            f"- {candidate.symbol}: 价格{candidate.price:.8g}, 24h涨幅{candidate.change_pct_24h * 100:.2f}%, "
            f"振幅{candidate.amplitude_pct_24h * 100:.2f}%, 得分{candidate.total_score:.2f}, 原因{candidate.reason}"
        )
    lines.extend(["策略经验:", strategy_memory or "- 暂无"])
    return "\n".join(lines)


def _attribution_prompt(symbol: str, pnl_usdt: float, return_pct: float, summary: str, strategy_memory: str = "") -> str:
    return "\n".join(
        [
            "为一次模拟盘交易做归因。只输出 JSON。",
            'JSON格式: {"category":"追高|假突破|流动性不足|新闻误判|止损太紧|止盈太早|入场太晚|执行失败|未知","confidence":0.0,"reason":"中文原因"}',
            f"币种: {symbol}",
            f"盈亏: {pnl_usdt:+.2f} USDT",
            f"收益率: {return_pct:+.2f}%",
            f"事件摘要: {summary}",
            "策略经验:",
            strategy_memory or "- 暂无",
        ]
    )


def _json_repair_prompt(raw_text: str, schema: str | None = None) -> str:
    return "\n".join(
        [
            "把下面内容修复成严格 JSON，不要解释，不要 Markdown。",
            f"必须符合: {schema or _trade_decision_schema()}",
            "原始内容:",
            raw_text[:4000],
        ]
    )


def _trade_decision_schema() -> str:
    return (
        '{"action":"buy|hold|sell","entry_mode":"market_now|limit_pullback|split_limit|breakout_confirm|wait",'
        '"exit_mode":"hold|sell_all|sell_partial|trail_profit|move_to_breakeven",'
        '"size_mode":"explore|normal|strong|reduced","stop_mode":"fixed|wide|tight|breakeven|trailing",'
        '"replace_mode":"none|replace_weakest|free_cash_only","confidence":0.0,"reason":"中文原因"}'
    )


def _market_regime_schema() -> str:
    return '{"regime":"单边上涨|震荡|急跌反弹|高波动插针|主流吸血|山寨轮动","confidence":0.0,"reason":"中文原因"}'


def _attribution_schema() -> str:
    return '{"category":"追高|假突破|流动性不足|新闻误判|止损太紧|止盈太早|入场太晚|执行失败|未知","confidence":0.0,"reason":"中文原因"}'


def _scan_review_schema() -> str:
    return '{"summary":"中文资金结论"}'


def _parse_failed_error(source: str, error: str) -> str:
    source_map = {
        "buy": "buy_parse_failed",
        "sell": "sell_parse_failed",
        "training": "training_parse_failed",
        "portfolio_training": "training_parse_failed",
        "market_regime": "market_regime_parse_failed",
        "attribution": "attribution_parse_failed",
        "scan": "scan_parse_failed",
    }
    return f"{source_map.get(source, source + '_parse_failed')}: {error}"


def _candidate_lines(candidate: CandidateScore, signals: list[InfoSignal]) -> list[str]:
    lines = [
        (
            f"- {candidate.symbol}: price={candidate.price:.8g}, "
            f"24h_change={candidate.change_pct_24h * 100:.2f}%, "
            f"24h_amp={candidate.amplitude_pct_24h * 100:.2f}%, "
            f"quote_vol={candidate.volume_quote_24h:.2f}, score={candidate.total_score:.2f}, "
            f"confirmed={candidate.confirmed}, reason={candidate.reason}"
        )
    ]
    lines.extend(_signal_lines(signals, limit=5))
    return lines


def _signal_lines(signals: list[InfoSignal], limit: int) -> list[str]:
    if not signals:
        return ["- 暂无"]
    return [f"- [{s.source}] score={s.score:.2f} {s.title}" for s in signals[:limit]]


def _group_info(signals: list[InfoSignal]) -> dict[str, list[InfoSignal]]:
    grouped: dict[str, list[InfoSignal]] = {}
    for signal in signals:
        grouped.setdefault(signal.symbol, []).append(signal)
    return grouped


def _parse_trade_decision(text: str) -> AiTradeDecision:
    raw_text = text
    payload = _json_payload(text)
    if payload is None:
        return AiTradeDecision(False, raw_text=raw_text, error="AI 决策 JSON 解析失败。")
    action = _choice(payload.get("action"), {"buy", "hold", "sell"}, "hold")
    confidence = _confidence(payload.get("confidence"))
    return AiTradeDecision(
        True,
        action=action,
        confidence=confidence,
        reason=str(payload.get("reason") or "")[:500],
        raw_text=raw_text,
        entry_mode=_choice(payload.get("entry_mode"), ENTRY_MODES, "market_now" if action == "buy" else "wait"),
        exit_mode=_choice(payload.get("exit_mode"), EXIT_MODES, "sell_all" if action == "sell" else "hold"),
        size_mode=_choice(payload.get("size_mode"), SIZE_MODES, "normal"),
        stop_mode=_choice(payload.get("stop_mode"), STOP_MODES, "fixed"),
        replace_mode=_choice(payload.get("replace_mode"), REPLACE_MODES, "none"),
    )


def _parse_scan_review(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    payload = _json_payload(stripped)
    if payload is None:
        if "{" in stripped or "}" in stripped:
            return None
        return stripped
    for key in ("summary", "reason", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_market_regime(text: str) -> AiMarketRegime:
    payload = _json_payload(text)
    if payload is None:
        return AiMarketRegime(False, error="AI 行情状态 JSON 解析失败。")
    return AiMarketRegime(
        True,
        regime=_choice(payload.get("regime"), MARKET_REGIMES, "震荡"),
        confidence=_confidence(payload.get("confidence")),
        reason=str(payload.get("reason") or "")[:500],
        raw_text=text,
    )


def _parse_trade_attribution(text: str) -> AiTradeAttribution:
    payload = _json_payload(text)
    if payload is None:
        return AiTradeAttribution(False, error="AI 归因 JSON 解析失败。")
    return AiTradeAttribution(
        True,
        category=_choice(payload.get("category"), ATTRIBUTION_CATEGORIES, "未知"),
        confidence=_confidence(payload.get("confidence")),
        reason=str(payload.get("reason") or "")[:500],
        raw_text=text,
    )


def _json_payload(text: str) -> dict | None:
    text = text.strip()
    if "```" in text:
        text = text.replace("```json", "```")
        text = next((part.strip() for part in text.split("```") if "{" in part and "}" in part), text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _choice(value, allowed: set[str], default: str) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in allowed else default


def _confidence(value) -> float:
    try:
        confidence = float(value or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(confidence, 1.0))


def _extract_ai_text(payload: dict) -> str:
    anthropic_text = _extract_anthropic_text(payload)
    if anthropic_text:
        return anthropic_text
    chat_text = _extract_chat_text(payload)
    if chat_text:
        return chat_text
    return _extract_output_text(payload)


def _extract_anthropic_text(payload: dict) -> str:
    chunks: list[str] = []
    for item in payload.get("content", []):
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def _extract_chat_text(payload: dict) -> str:
    chunks: list[str] = []
    for choice in payload.get("choices", []):
        message = choice.get("message", {})
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                text = item.get("text") if isinstance(item, dict) else None
                if isinstance(text, str):
                    chunks.append(text)
        if not chunks:
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                chunks.append(_summarize_reasoning_fallback(reasoning))
    return "\n".join(chunks)


def _summarize_reasoning_fallback(text: str) -> str:
    compact = " ".join(text.split())
    if any(word in compact for word in ("停止", "风险偏高", "不建议", "暂不")):
        return '{"action":"hold","confidence":0.7,"reason":"模型推理认为风险偏高，暂不交易"}'
    if any(word in compact for word in ("买入", "允许", "风险可控", "继续运行")):
        return '{"action":"buy","entry_mode":"market_now","confidence":0.7,"reason":"模型推理认为风险可控"}'
    return '{"action":"hold","confidence":0.5,"reason":"模型只返回推理，未给出明确交易结论"}'


def _extract_output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _extract_usage(payload: dict) -> dict[str, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 1.8))


def _telegram_sized(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...(已截断)"


def _urlopen_bytes(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()
