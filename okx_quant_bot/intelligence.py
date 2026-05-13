from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

from okx_quant_bot.config import Settings
from okx_quant_bot.models import InfoSignal, IntelligenceItem


DEFAULT_NEWS_RSS_URLS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
)

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


CATALYST_WORDS = {
    "listing": 2.0,
    "listed": 2.0,
    "launch": 1.5,
    "mainnet": 1.5,
    "partnership": 1.2,
    "etf": 1.5,
    "hack": -2.0,
    "exploit": -2.0,
    "delist": -2.5,
    "lawsuit": -1.5,
    "sec": -1.2,
}


@dataclass(frozen=True)
class IntelligenceScan:
    items: list[IntelligenceItem]

    @property
    def signals(self) -> list[InfoSignal]:
        return [
            InfoSignal(item.source, item.symbol, item.score, item.title, item.url)
            for item in self.items
            if item.score > 0
        ]


class IntelligenceRadar:
    def __init__(self, settings: Settings, timeout: float = 8.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def scan(self, symbols: Iterable[str]) -> IntelligenceScan:
        symbols = tuple(symbols)
        items: list[IntelligenceItem] = []
        items.extend(self._scan_rss(symbols))
        items.extend(self._scan_cryptopanic(symbols))
        items.extend(self._scan_coinmarketcal(symbols))
        deduped = _dedupe(items)
        return IntelligenceScan(deduped[: self.settings.intelligence_max_items])

    def _scan_rss(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        urls = self.settings.news_rss_urls or (
            DEFAULT_NEWS_RSS_URLS if self.settings.news_scan_aggressive else ()
        )
        items: list[IntelligenceItem] = []
        for url in urls:
            try:
                raw = _http_get(url, timeout=self.timeout)
                root = ET.fromstring(raw)
            except Exception:
                continue
            nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for node in nodes[:100]:
                title = _child_text(node, "title")
                link = _child_text(node, "link") or url
                if not title:
                    continue
                for symbol in symbols:
                    if _matches_symbol(title, symbol):
                        items.append(_item("rss", symbol, title, link, {"url": url}))
        return items

    def _scan_cryptopanic(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        if not self.settings.cryptopanic_auth_token:
            return []
        url = self.settings.cryptopanic_base_url + "?" + urllib.parse.urlencode(
            {"auth_token": self.settings.cryptopanic_auth_token, "public": "true"}
        )
        try:
            payload = json.loads(_http_get(url, timeout=self.timeout))
        except Exception:
            return []
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        items: list[IntelligenceItem] = []
        for row in rows[:100]:
            title = str(row.get("title") or "")
            link = str(row.get("url") or row.get("domain") or "")
            currencies = {
                str(c.get("code") or "").upper()
                for c in row.get("currencies", [])
                if isinstance(c, dict)
            }
            for symbol in symbols:
                base = _base(symbol)
                if base in currencies or _matches_symbol(title, symbol):
                    items.append(_item("cryptopanic", symbol, title, link, row))
        return items

    def _scan_coinmarketcal(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        if not self.settings.coinmarketcal_api_key:
            return []
        url = "https://developers.coinmarketcal.com/v1/events?" + urllib.parse.urlencode(
            {"max": str(min(self.settings.intelligence_max_items, 75)), "sortBy": "trending_events"}
        )
        try:
            raw = _http_get(
                url,
                timeout=self.timeout,
                headers={
                    "x-api-key": self.settings.coinmarketcal_api_key,
                    "Accept": "application/json",
                },
            )
            payload = json.loads(raw)
        except Exception:
            return []
        rows = payload.get("body", []) if isinstance(payload, dict) else []
        items: list[IntelligenceItem] = []
        for row in rows:
            title = _localized(row.get("title")) or str(row.get("title") or "")
            link = str(row.get("source") or "")
            coins = row.get("coins", []) if isinstance(row, dict) else []
            coin_symbols = {str(c.get("symbol") or c.get("name") or "").upper() for c in coins}
            for symbol in symbols:
                base = _base(symbol)
                if base in coin_symbols or _matches_symbol(title, symbol):
                    items.append(_item("coinmarketcal", symbol, title, link, row))
        return items


def _item(source: str, symbol: str, title: str, url: str, raw) -> IntelligenceItem:
    return IntelligenceItem(
        source=source,
        symbol=symbol,
        title=title[:240],
        url=url[:500],
        score=_score_title(title),
        raw=json.dumps(raw, ensure_ascii=False)[:4000],
    )


def _score_title(title: str) -> float:
    text = title.lower()
    score = 1.0
    for word, weight in CATALYST_WORDS.items():
        if word in text:
            score += weight
    return max(score, -3.0)


def _matches_symbol(text: str, symbol: str) -> bool:
    base = _base(symbol)
    haystack = text.lower()
    aliases = {base.lower(), TOKEN_NAMES.get(base, "").lower()}
    return any(alias and f" {alias}" in f" {haystack}" for alias in aliases)


def _base(symbol: str) -> str:
    return symbol.split("-", 1)[0].upper()


def _child_text(item: ET.Element, name: str) -> str:
    child = item.find(name)
    if child is None:
        child = item.find(f"{{http://www.w3.org/2005/Atom}}{name}")
    if child is None:
        return ""
    if name == "link" and child.text is None:
        return str(child.attrib.get("href", ""))
    return (child.text or "").strip()


def _localized(value) -> str:
    if isinstance(value, dict):
        return str(value.get("en") or next(iter(value.values()), ""))
    return ""


def _dedupe(items: list[IntelligenceItem]) -> list[IntelligenceItem]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[IntelligenceItem] = []
    for item in sorted(items, key=lambda i: i.score, reverse=True):
        key = (item.source, item.symbol, item.title.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _http_get(url: str, timeout: float, headers: dict[str, str] | None = None) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "okx-quant-bot/0.1", **(headers or {})},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")
