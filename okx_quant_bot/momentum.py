from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

from okx_quant_bot.config import Settings
from okx_quant_bot.exchange import OkxRestClient
from okx_quant_bot.models import CandidateScore, InfoSignal, MarketTicker, StopLossPlan


STABLE_BASES = {
    "USDT",
    "USDC",
    "DAI",
    "TUSD",
    "FDUSD",
    "USDD",
    "USDG",
    "PYUSD",
    "EURT",
}

TOKEN_NAMES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "BNB": "binance coin",
    "AVAX": "avalanche",
    "LINK": "chainlink",
    "DOT": "polkadot",
    "TON": "toncoin",
}

DEFAULT_NEWS_RSS_URLS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
)


@dataclass(frozen=True)
class MomentumScan:
    tickers: list[MarketTicker]
    info_signals: list[InfoSignal]
    candidates: list[CandidateScore]

    @property
    def best(self) -> CandidateScore | None:
        return self.candidates[0] if self.candidates else None


class MarketScanner:
    def __init__(self, exchange: OkxRestClient, settings: Settings) -> None:
        self.exchange = exchange
        self.settings = settings

    def top_momentum_tickers(self) -> list[MarketTicker]:
        tickers = [
            ticker
            for ticker in self.exchange.get_market_tickers("SPOT")
            if _is_tradeable_usdt_symbol(ticker.symbol)
            and ticker.change_pct_24h > 0
            and ticker.amplitude_pct_24h > 0
            and ticker.volume_quote_24h > 0
        ]
        by_gainers = sorted(
            tickers,
            key=lambda t: (t.change_pct_24h, t.volume_quote_24h),
            reverse=True,
        )[: self.settings.candidate_top_n]
        return sorted(
            by_gainers,
            key=lambda t: (t.amplitude_pct_24h, t.change_pct_24h, t.volume_quote_24h),
            reverse=True,
        )


class NewsSignalClient:
    def __init__(self, urls: Iterable[str], timeout: float = 8.0) -> None:
        self.urls = tuple(urls)
        self.timeout = timeout

    def fetch(self, symbols: Iterable[str]) -> list[InfoSignal]:
        if not self.urls:
            return []
        symbols = tuple(symbols)
        signals: list[InfoSignal] = []
        for url in self.urls:
            try:
                raw = _http_get(url, timeout=self.timeout)
                signals.extend(self._parse_feed(raw, url, symbols))
            except Exception:
                continue
        return signals

    def _parse_feed(self, raw: str, feed_url: str, symbols: tuple[str, ...]) -> list[InfoSignal]:
        root = ET.fromstring(raw)
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        signals: list[InfoSignal] = []
        for item in items[:100]:
            title = _child_text(item, "title")
            link = _child_text(item, "link") or feed_url
            if not title:
                continue
            for symbol in symbols:
                if _matches_symbol(title, symbol):
                    signals.append(InfoSignal("news", symbol, 1.0, title[:240], link))
        return signals


class PolymarketSignalClient:
    def __init__(self, enabled: bool = True, timeout: float = 8.0) -> None:
        self.enabled = enabled
        self.timeout = timeout

    def fetch(self, symbols: Iterable[str]) -> list[InfoSignal]:
        if not self.enabled:
            return []
        signals: list[InfoSignal] = []
        for symbol in symbols:
            base = _base_symbol(symbol)
            query = TOKEN_NAMES.get(base, base)
            url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode(
                {"search": query, "active": "true", "closed": "false", "limit": "20"}
            )
            try:
                payload = json.loads(_http_get(url, timeout=self.timeout))
            except Exception:
                continue
            markets = payload if isinstance(payload, list) else payload.get("markets", [])
            for market in markets[:5]:
                title = str(market.get("question") or market.get("title") or "")
                volume = _floatish(market.get("volume") or market.get("volumeNum") or 0)
                if title and _is_crypto_market(title, base):
                    score = 1.0 + min(volume / 100000.0, 3.0)
                    signals.append(
                        InfoSignal(
                            "polymarket",
                            symbol,
                            score,
                            title[:240],
                            str(market.get("slug") or market.get("conditionId") or ""),
                        )
                    )
                    break
        return signals


class CandidateScorer:
    def __init__(self, require_info_confirmation: bool = False) -> None:
        self.require_info_confirmation = require_info_confirmation

    def score(
        self,
        tickers: Iterable[MarketTicker],
        info_signals: Iterable[InfoSignal],
    ) -> list[CandidateScore]:
        signals_by_symbol: dict[str, list[InfoSignal]] = {}
        for signal in info_signals:
            signals_by_symbol.setdefault(signal.symbol, []).append(signal)

        candidates: list[CandidateScore] = []
        for ticker in tickers:
            signals = signals_by_symbol.get(ticker.symbol, [])
            news_score = sum(s.score for s in signals if s.source == "news")
            polymarket_score = sum(s.score for s in signals if s.source == "polymarket")
            market_score = (
                ticker.change_pct_24h * 100.0
                + ticker.amplitude_pct_24h * 50.0
                + math.log10(max(ticker.volume_quote_24h, 1.0))
            )
            confirmed = news_score > 0 or not self.require_info_confirmation
            total_score = market_score + news_score * 4.0
            reason = _candidate_reason(ticker, news_score, polymarket_score, confirmed)
            candidates.append(
                CandidateScore(
                    symbol=ticker.symbol,
                    price=ticker.last,
                    change_pct_24h=ticker.change_pct_24h,
                    amplitude_pct_24h=ticker.amplitude_pct_24h,
                    volume_quote_24h=ticker.volume_quote_24h,
                    news_score=news_score,
                    polymarket_score=polymarket_score,
                    total_score=total_score,
                    reason=reason,
                    confirmed=confirmed,
                )
            )

        return sorted(candidates, key=lambda c: c.total_score, reverse=True)


def run_momentum_scan(settings: Settings, exchange: OkxRestClient) -> MomentumScan:
    scanner = MarketScanner(exchange, settings)
    tickers = scanner.top_momentum_tickers()
    symbols = [ticker.symbol for ticker in tickers]
    news_urls = settings.news_rss_urls or (DEFAULT_NEWS_RSS_URLS if settings.news_scan_aggressive else ())
    news_signals = NewsSignalClient(news_urls).fetch(symbols)
    polymarket_signals = PolymarketSignalClient(settings.polymarket_enabled).fetch(symbols)
    info_signals = news_signals + polymarket_signals
    candidates = CandidateScorer(settings.require_info_confirmation).score(tickers, info_signals)
    return MomentumScan(tickers=tickers, info_signals=info_signals, candidates=candidates)


def target_position_usdt(settings: Settings) -> float:
    risk_sized = settings.risk_per_trade_usdt / settings.initial_stop_loss_pct
    if settings.target_position_usdt > 0:
        return min(settings.target_position_usdt, risk_sized)
    return risk_sized


def stop_loss_plan(settings: Settings, symbol: str, entry_price: float, quote_amount: float) -> StopLossPlan:
    size = 0.0 if entry_price <= 0 else quote_amount / entry_price
    if settings.stop_mode == "fixed_loss":
        stop_distance = settings.fixed_stop_loss_usdt / size if size > 0 else 0.0
        stop_price = max(entry_price - stop_distance, 0.0)
        risk = settings.fixed_stop_loss_usdt
    else:
        stop_price = entry_price * (1.0 - settings.initial_stop_loss_pct)
        risk = quote_amount * settings.initial_stop_loss_pct
    return StopLossPlan(
        symbol=symbol,
        entry_price=entry_price,
        size=size,
        stop_price=stop_price,
        risk_usdt=risk,
        mode=settings.stop_mode,
    )


def _is_tradeable_usdt_symbol(symbol: str) -> bool:
    if not symbol.endswith("-USDT"):
        return False
    base = _base_symbol(symbol)
    return base not in STABLE_BASES and not base.endswith(("3L", "3S", "5L", "5S"))


def _base_symbol(symbol: str) -> str:
    return symbol.split("-", 1)[0].upper()


def _matches_symbol(text: str, symbol: str) -> bool:
    base = _base_symbol(symbol)
    haystack = text.lower()
    aliases = {base.lower(), TOKEN_NAMES.get(base, "").lower()}
    return any(alias and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", haystack) for alias in aliases)


def _is_crypto_market(title: str, base: str) -> bool:
    haystack = title.lower()
    token_name = TOKEN_NAMES.get(base, "").lower()
    return base.lower() in haystack or bool(token_name and token_name in haystack)


def _candidate_reason(
    ticker: MarketTicker,
    news_score: float,
    polymarket_score: float,
    confirmed: bool,
) -> str:
    confirm = "信息面已确认" if confirmed else "缺少新闻/Polymarket确认"
    return (
        f"{confirm}；24h涨幅{ticker.change_pct_24h * 100:.2f}%，"
        f"振幅{ticker.amplitude_pct_24h * 100:.2f}%，"
        f"新闻分{news_score:.1f}，Polymarket分{polymarket_score:.1f}"
    )


def _http_get(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "okx-quant-bot/0.1"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _child_text(item: ET.Element, name: str) -> str:
    child = item.find(name)
    if child is None:
        child = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
    if child is None:
        return ""
    if name == "link" and child.text is None:
        return str(child.attrib.get("href", ""))
    return (child.text or "").strip()


def _floatish(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
