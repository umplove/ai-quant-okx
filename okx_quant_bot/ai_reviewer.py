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

    def review_scan(self, scan: MomentumScan, open_position_count: int) -> AiReview:
        if not self.enabled:
            return AiReview(False, "", "AI review is disabled or OPENAI_API_KEY is missing.")

        body = {
            "model": self.settings.openai_model,
            "instructions": _instructions(),
            "input": _scan_prompt(self.settings, scan, open_position_count),
            "max_output_tokens": 700,
        }
        request = urllib.request.Request(
            f"{self.settings.openai_base_url}/responses",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "okx-quant-bot/0.1",
            },
        )

        try:
            raw = self._opener(request, 30.0)
            payload = json.loads(raw.decode("utf-8"))
            text = _extract_output_text(payload).strip()
            if not text:
                return AiReview(False, "", "OpenAI response did not contain output text.")
            return AiReview(True, _telegram_sized(text))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            return AiReview(False, "", f"OpenAI HTTP {exc.code}: {detail}")
        except Exception as exc:
            return AiReview(False, "", f"OpenAI review failed: {exc}")


def _instructions() -> str:
    return (
        "你是一个谨慎的加密货币量化复盘助手，只做风险审查和复盘，不给确定性收益承诺。"
        "输出中文，简洁，最多6条要点。必须明确区分事实、推断和建议。"
        "不要要求扩大仓位，不要建议移除止损，不要声称一定盈利。"
    )


def _scan_prompt(settings: Settings, scan: MomentumScan, open_position_count: int) -> str:
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
        "候选币:",
    ]
    if not candidates:
        lines.append("- 无候选币")
    for candidate in candidates:
        lines.extend(_candidate_lines(candidate, info_by_symbol.get(candidate.symbol, [])))
    lines.extend(
        [
            "请输出:",
            "1. 本轮市场状态",
            "2. 最值得关注的候选和理由",
            "3. 最大风险",
            "4. 是否建议继续观望、仅记录、或允许规则策略按原风控执行",
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
