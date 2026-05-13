from __future__ import annotations

import json
import re
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
    "https://cryptoslate.com/feed/",
    "https://www.newsbtc.com/feed/",
    "https://bitcoinmagazine.com/.rss/full/",
)

COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
ALTERNATIVE_FNG_URL = "https://api.alternative.me/fng/?limit=1"
BINANCE_ANNOUNCEMENTS_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?"
    + urllib.parse.urlencode({"catalogId": "48", "pageNo": "1", "pageSize": "40"})
)
OKX_ANNOUNCEMENTS_URL = "https://www.okx.com/en-us/help/category/announcements"

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
    "upgrade": 1.0,
    "airdrop": 1.5,
    "burn": 1.0,
    "integration": 1.0,
    "etf": 1.5,
    "hack": -2.0,
    "exploit": -2.0,
    "delist": -2.5,
    "suspend": -1.5,
    "outage": -1.5,
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
        items.extend(self._scan_coingecko_trending(symbols))
        items.extend(self._scan_fear_greed(symbols))
        items.extend(self._scan_binance_announcements(symbols))
        items.extend(self._scan_okx_announcements(symbols))
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

    def _scan_coingecko_trending(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        if not self.settings.news_scan_aggressive:
            return []
        try:
            payload = json.loads(_http_get(COINGECKO_TRENDING_URL, timeout=self.timeout))
        except Exception:
            return []
        rows = payload.get("coins", []) if isinstance(payload, dict) else []
        items: list[IntelligenceItem] = []
        for rank, row in enumerate(rows[:30], start=1):
            item = row.get("item", {}) if isinstance(row, dict) else {}
            base = str(item.get("symbol") or "").upper()
            name = str(item.get("name") or base)
            for symbol in symbols:
                if _base(symbol) == base:
                    title = f"{base} trending on CoinGecko search: {name}"
                    score = max(1.0, 2.5 - rank * 0.05)
                    items.append(_item("coingecko_trending", symbol, title, COINGECKO_TRENDING_URL, item, score))
        return items

    def _scan_fear_greed(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        if not self.settings.news_scan_aggressive or not symbols:
            return []
        try:
            payload = json.loads(_http_get(ALTERNATIVE_FNG_URL, timeout=self.timeout))
        except Exception:
            return []
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows:
            return []
        row = rows[0]
        value = _floatish(row.get("value"))
        label = str(row.get("value_classification") or "unknown")
        score = _fear_greed_score(value, label)
        if score <= 0:
            return []
        symbol = "BTC-USDT" if "BTC-USDT" in symbols else symbols[0]
        title = f"Crypto Fear & Greed Index {value:.0f}: {label}"
        return [_item("alternative_fng", symbol, title, ALTERNATIVE_FNG_URL, row, score)]

    def _scan_binance_announcements(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        if not self.settings.news_scan_aggressive:
            return []
        try:
            payload = json.loads(_http_get(BINANCE_ANNOUNCEMENTS_URL, timeout=self.timeout))
        except Exception:
            return []
        articles = _find_article_dicts(payload)
        items: list[IntelligenceItem] = []
        for article in articles[:80]:
            title = str(article.get("title") or article.get("name") or "")
            if not title:
                continue
            link = _announcement_link("https://www.binance.com/en/support/announcement", article)
            for symbol in symbols:
                if _matches_symbol(title, symbol):
                    items.append(_item("binance_announcement", symbol, title, link, article))
        return items

    def _scan_okx_announcements(self, symbols: tuple[str, ...]) -> list[IntelligenceItem]:
        if not self.settings.news_scan_aggressive:
            return []
        try:
            html = _http_get(OKX_ANNOUNCEMENTS_URL, timeout=self.timeout)
        except Exception:
            return []
        titles = _extract_html_titles(html)
        items: list[IntelligenceItem] = []
        for title in titles[:80]:
            for symbol in symbols:
                if _matches_symbol(title, symbol):
                    items.append(_item("okx_announcement", symbol, title, OKX_ANNOUNCEMENTS_URL, {"title": title}))
        return items


def _item(source: str, symbol: str, title: str, url: str, raw, score: float | None = None) -> IntelligenceItem:
    return IntelligenceItem(
        source=source,
        symbol=symbol,
        title=title[:240],
        url=url[:500],
        score=_score_title(title) if score is None else score,
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


def _fear_greed_score(value: float, label: str) -> float:
    lower = label.lower()
    if value >= 75 or "extreme greed" in lower:
        return 0.4
    if value >= 55 or "greed" in lower:
        return 1.0
    if value >= 45 or "neutral" in lower:
        return 0.3
    return 0.0


def _find_article_dicts(payload) -> list[dict]:
    found: list[dict] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            title = value.get("title") or value.get("name")
            if title:
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def _announcement_link(base_url: str, article: dict) -> str:
    path = str(article.get("code") or article.get("id") or article.get("slug") or "")
    return f"{base_url}/{path}" if path else base_url


def _extract_html_titles(html: str) -> list[str]:
    text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;|&#x27;|&quot;|&amp;", " ", text)
    titles: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        title = " ".join(line.split())
        if len(title) < 12 or len(title) > 180:
            continue
        lower = title.lower()
        if not any(word in lower for word in ("list", "launch", "spot", "trading", "delist", "support")):
            continue
        if title.lower() in seen:
            continue
        seen.add(title.lower())
        titles.append(title)
    return titles


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
