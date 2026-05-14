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
    retry_count: int = 0


@dataclass(frozen=True)
class AiTradeDecision:
    ok: bool
    action: str = "hold"
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
    retry_count: int = 0

    @property
    def approved_buy(self) -> bool:
        return self.ok and self.action == "buy" and self.confidence >= 0.65

    @property
    def approved_sell(self) -> bool:
        return self.ok and self.action == "sell" and self.confidence >= 0.65


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
            return AiReview(False, "", "AI未启用或缺少OPENAI_API_KEY/MIMO_API_KEY。")
        prompt = _scan_prompt(self.settings, scan, open_position_count, strategy_memory)
        result = self._complete(prompt)
        return AiReview(
            ok=result.ok,
            text=_telegram_sized(result.raw_text) if result.ok else "",
            error="" if result.ok else result.error,
            prompt_chars=result.prompt_chars,
            response_chars=result.response_chars,
            duration_ms=result.duration_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            retry_count=result.retry_count,
        )

    def decide_buy(
        self,
        scan: MomentumScan,
        candidate: CandidateScore,
        open_position_count: int,
        strategy_memory: str = "",
    ) -> AiTradeDecision:
        prompt = _buy_prompt(self.settings, scan, candidate, open_position_count, strategy_memory)
        return self._decide(prompt)

    def decide_sell(
        self,
        scan: MomentumScan,
        position: Position,
        current_price: float,
        strategy_memory: str = "",
    ) -> AiTradeDecision:
        prompt = _sell_prompt(self.settings, scan, position, current_price, strategy_memory)
        return self._decide(prompt)

    def complete_training(self, prompt: str) -> AiTradeDecision:
        return self._complete(prompt)

    def _decide(self, prompt: str) -> AiTradeDecision:
        result = self._complete(prompt)
        if not result.ok:
            return result
        parsed = _parse_trade_decision(result.raw_text)
        return AiTradeDecision(
            ok=parsed.ok,
            action=parsed.action,
            confidence=parsed.confidence,
            reason=parsed.reason,
            raw_text=parsed.raw_text,
            error=parsed.error,
            prompt_chars=result.prompt_chars,
            response_chars=result.response_chars,
            duration_ms=result.duration_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            retry_count=result.retry_count,
        )

    def _complete(self, prompt: str) -> AiTradeDecision:
        if not self.enabled:
            return AiTradeDecision(False, error="AI未启用或缺少OPENAI_API_KEY/MIMO_API_KEY。", prompt_chars=len(prompt))
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
                    raise ValueError("AI响应没有正文。")
                return AiTradeDecision(
                    True,
                    raw_text=text,
                    prompt_chars=len(prompt),
                    response_chars=len(text),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
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
        "你是加密货币模拟盘交易训练助手。交易决策必须只输出 JSON，不要 Markdown。"
        "JSON 格式: {\"action\":\"buy|hold|sell\",\"confidence\":0.0,\"reason\":\"一句中文原因\"}。"
        "原因必须使用中文。不要承诺一定盈利。不要建议移除止损。"
    )


def _scan_prompt(
    settings: Settings,
    scan: MomentumScan,
    open_position_count: int,
    strategy_memory: str = "",
) -> str:
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
) -> str:
    info_by_symbol = _group_info(scan.info_signals)
    lines = [
        "判断是否允许买入这个候选币。只输出 JSON。",
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
) -> str:
    pnl = (current_price - position.avg_entry_price) * position.base_qty
    return_pct = 0.0 if position.avg_entry_price <= 0 else (current_price - position.avg_entry_price) / position.avg_entry_price * 100
    info_by_symbol = _group_info(scan.info_signals)
    lines = [
        "判断当前持仓是否应该市价卖出。只输出 JSON。",
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
    text = text.strip()
    if "```" in text:
        text = text.replace("```json", "```")
        text = next((part.strip() for part in text.split("```") if "action" in part), text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return AiTradeDecision(False, raw_text=raw_text, error="AI决策JSON解析失败。")
    action = str(payload.get("action") or "hold").strip().lower()
    if action not in {"buy", "hold", "sell"}:
        action = "hold"
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    reason = str(payload.get("reason") or "")[:500]
    return AiTradeDecision(True, action, confidence, reason, raw_text)


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
        return '{"action":"buy","confidence":0.7,"reason":"模型推理认为风险可控"}'
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
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _telegram_sized(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...(已截断)"


def _urlopen_bytes(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()
