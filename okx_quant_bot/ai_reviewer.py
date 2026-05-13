from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from okx_quant_bot.config import Settings
from okx_quant_bot.models import CandidateScore, InfoSignal
from okx_quant_bot.momentum import MomentumScan


@dataclass(frozen=True)
class AiReview:
    ok: bool
    text: str
    error: str = ""


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
            return AiReview(False, "", "AI review is disabled or OPENAI_API_KEY is missing.")

        prompt = _scan_prompt(self.settings, scan, open_position_count, strategy_memory)
        body = self._request_body(prompt)
        request = urllib.request.Request(
            self._request_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )

        try:
            raw = self._opener(request, self.settings.ai_review_timeout_seconds)
            payload = json.loads(raw.decode("utf-8"))
            text = _extract_ai_text(payload).strip()
            if not text:
                return AiReview(False, "", "OpenAI response did not contain output text.")
            return AiReview(True, _telegram_sized(text))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            return AiReview(False, "", f"OpenAI HTTP {exc.code}: {detail}")
        except TimeoutError:
            return AiReview(False, "", "timeout")
        except Exception as exc:
            if "timed out" in str(exc).lower():
                return AiReview(False, "", "timeout")
            return AiReview(False, "", f"OpenAI review failed: {exc}")

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
            return {
                "model": self.settings.openai_model,
                "messages": [
                    {"role": "system", "content": _instructions()},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": self.settings.ai_review_max_tokens,
            }
        return {
            "model": self.settings.openai_model,
            "instructions": _instructions(),
            "input": prompt,
            "max_output_tokens": self.settings.ai_review_max_tokens,
        }

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "okx-quant-bot/0.1",
        }
        if self.settings.openai_api_mode == "anthropic":
            headers["x-api-key"] = self.settings.openai_api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {self.settings.openai_api_key}"
        return headers


def _instructions() -> str:
    return (
        "你是一个加密货币模拟盘资金复盘助手。只关心钱：权益、盈亏、风险敞口、是否应该继续让规则策略运行。"
        "输出中文，必须先给最终结论，最多3句话。不要列候选币长名单，不要讲无关市场新闻。"
        "不要要求扩大仓位，不要建议移除止损，不要声称一定盈利。"
    )


def _scan_prompt(
    settings: Settings,
    scan: MomentumScan,
    open_position_count: int,
    strategy_memory: str = "",
) -> str:
    candidates = scan.candidates[: settings.ai_review_max_candidates]
    info_by_symbol = _group_info(scan.info_signals)
    lines = [
        "请复盘本轮 OKX 现货动量扫描。",
        f"模式: {'模拟盘' if settings.okx_demo else '实盘'}; "
        f"真实下单: {'开启' if settings.trading_enabled else '关闭'}; "
        f"当前持仓数: {open_position_count}/{settings.max_open_positions}",
        f"单笔目标名义: {settings.target_position_usdt:.2f} USDT; "
        f"单笔风险预算: {settings.risk_per_trade_usdt:.2f} USDT; "
        f"止损模式: {settings.stop_mode}",
        f"扫描行情数: {len(scan.tickers)}; 信息信号数: {len(scan.info_signals)}",
        "有效策略记忆:",
        strategy_memory or "- 暂无",
        "候选币:",
    ]
    if not candidates:
        lines.append("- 无候选币")
    for candidate in candidates:
        lines.extend(_candidate_lines(candidate, info_by_symbol.get(candidate.symbol, [])))
    lines.extend(
        [
            "请输出:",
            "1. 资金风险结论",
            "2. 是否允许规则策略按原风控继续运行",
            "3. 如风险偏高，直接说停止",
        ]
    )
    return "\n".join(lines)


def _candidate_lines(candidate: CandidateScore, signals: list[InfoSignal]) -> list[str]:
    lines = [
        (
            f"- {candidate.symbol}: price={candidate.price:.8g}, "
            f"24h_change={candidate.change_pct_24h * 100:.2f}%, "
            f"24h_amp={candidate.amplitude_pct_24h * 100:.2f}%, "
            f"quote_vol={candidate.volume_quote_24h:.2f}, "
            f"score={candidate.total_score:.2f}, "
            f"confirmed={candidate.confirmed}, reason={candidate.reason}"
        )
    ]
    for signal in signals[:3]:
        lines.append(f"  info: [{signal.source}] score={signal.score:.2f} {signal.title}")
    return lines


def _group_info(signals: list[InfoSignal]) -> dict[str, list[InfoSignal]]:
    grouped: dict[str, list[InfoSignal]] = {}
    for signal in signals:
        grouped.setdefault(signal.symbol, []).append(signal)
    return grouped


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
        return "AI资金结论: 风险偏高，建议停止新交易。"
    if any(word in compact for word in ("允许", "继续运行", "风险低", "风险可控")):
        return "AI资金结论: 资金风险可控，允许规则策略按原风控继续运行。"
    return "AI资金结论: 已收到模型推理，但未生成最终结论，建议继续观察。"


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


def _telegram_sized(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...(已截断)"


def _urlopen_bytes(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()
