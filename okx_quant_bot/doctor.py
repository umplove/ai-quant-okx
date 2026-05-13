from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from okx_quant_bot.config import Settings
from okx_quant_bot.exchange import OkxRestClient


TELEGRAM_TEST_MESSAGE = "OKX量化机器人连通性检查通过"


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    name: str
    detail: str

    def line(self) -> str:
        return f"[{self.status.value}] {self.name}: {self.detail}"


class Doctor:
    def __init__(self, settings: Settings, client: OkxRestClient | None = None) -> None:
        self.settings = settings
        self.client = client or OkxRestClient(settings)

    def run(self, include_network: bool = True) -> list[CheckResult]:
        results = self.check_config()
        if include_network:
            results.extend(self.check_okx())
            results.append(self.check_telegram())
        return results

    def check_config(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        results.append(
            CheckResult(
                CheckStatus.PASS if Path(".env").exists() else CheckStatus.WARN,
                ".env",
                "found" if Path(".env").exists() else "not found; using process environment/defaults",
            )
        )
        credentials_ready = bool(
            self.settings.okx_api_key
            and self.settings.okx_secret_key
            and self.settings.okx_passphrase
        )
        results.append(
            CheckResult(
                CheckStatus.PASS if credentials_ready else CheckStatus.FAIL,
                "OKX credentials",
                "api key, secret, and passphrase are set"
                if credentials_ready
                else "missing api key, secret, or passphrase",
            )
        )
        results.append(
            CheckResult(
                CheckStatus.PASS if self.settings.okx_demo else CheckStatus.WARN,
                "OKX demo mode",
                "OKX_DEMO=true" if self.settings.okx_demo else "OKX_DEMO=false; live mode safety review required",
            )
        )
        results.append(
            CheckResult(
                CheckStatus.PASS if not self.settings.trading_enabled else CheckStatus.WARN,
                "Trading switch",
                "TRADING_ENABLED=false; no orders will be sent"
                if not self.settings.trading_enabled
                else "TRADING_ENABLED=true; doctor will not submit orders, but runner can",
            )
        )
        results.append(
            CheckResult(
                CheckStatus.PASS if not self.settings.allow_live_trading else CheckStatus.WARN,
                "Live trading guard",
                "ALLOW_LIVE_TRADING=false"
                if not self.settings.allow_live_trading
                else "ALLOW_LIVE_TRADING=true; live trading guard is open",
            )
        )
        expected_symbols = ("BTC-USDT", "ETH-USDT")
        results.append(
            CheckResult(
                CheckStatus.PASS if self.settings.symbols == expected_symbols else CheckStatus.WARN,
                "Symbols",
                ",".join(self.settings.symbols)
                if self.settings.symbols != expected_symbols
                else "BTC-USDT,ETH-USDT",
            )
        )
        telegram_ready = bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)
        results.append(
            CheckResult(
                CheckStatus.PASS if telegram_ready else CheckStatus.WARN,
                "Telegram config",
                "bot token and chat id are set" if telegram_ready else "missing token or chat id",
            )
        )
        return results

    def check_okx(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for symbol in self.settings.symbols:
            try:
                candles = self.client.get_candles(symbol, self.settings.bar, limit=2)
                if candles:
                    results.append(CheckResult(CheckStatus.PASS, f"OKX market {symbol}", "candles received"))
                else:
                    results.append(CheckResult(CheckStatus.FAIL, f"OKX market {symbol}", "empty candle response"))
            except Exception as exc:
                results.append(CheckResult(CheckStatus.FAIL, f"OKX market {symbol}", self._okx_hint(exc)))

        try:
            payload = self.client.get_balance("USDT")
            data = payload.get("data", [])
            results.append(
                CheckResult(
                    CheckStatus.PASS if data else CheckStatus.FAIL,
                    "OKX account auth",
                    "USDT balance response received" if data else "empty account balance response",
                )
            )
        except Exception as exc:
            results.append(CheckResult(CheckStatus.FAIL, "OKX account auth", self._okx_hint(exc)))

        try:
            payload = self.client.get_public_instruments("SPOT")
            instruments = {item.get("instId"): item for item in payload.get("data", [])}
            missing = [symbol for symbol in self.settings.symbols if symbol not in instruments]
            if missing:
                results.append(
                    CheckResult(CheckStatus.FAIL, "OKX spot instruments", f"missing: {','.join(missing)}")
                )
            else:
                parts = []
                for symbol in self.settings.symbols:
                    item = instruments[symbol]
                    parts.append(
                        f"{symbol} minSz={item.get('minSz')} tickSz={item.get('tickSz')} lotSz={item.get('lotSz')}"
                    )
                results.append(CheckResult(CheckStatus.PASS, "OKX spot instruments", "; ".join(parts)))
        except Exception as exc:
            results.append(CheckResult(CheckStatus.FAIL, "OKX spot instruments", self._okx_hint(exc)))
        return results

    def check_telegram(self) -> CheckResult:
        if not (self.settings.telegram_bot_token and self.settings.telegram_chat_id):
            return CheckResult(CheckStatus.WARN, "Telegram", "not configured; skipped")
        token = self.settings.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode(
            {"chat_id": self.settings.telegram_chat_id, "text": TELEGRAM_TEST_MESSAGE}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if 200 <= response.status < 300:
                    return CheckResult(CheckStatus.PASS, "Telegram", "test message sent")
                return CheckResult(CheckStatus.FAIL, "Telegram", f"unexpected HTTP status {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403, 404}:
                return CheckResult(
                    CheckStatus.FAIL,
                    "Telegram",
                    f"HTTP {exc.code}; check bot token, chat id, and whether the bot can message this chat",
                )
            return CheckResult(CheckStatus.FAIL, "Telegram", f"HTTP {exc.code}")
        except Exception:
            return CheckResult(CheckStatus.FAIL, "Telegram", "network error or timeout")

    @staticmethod
    def has_failures(results: list[CheckResult]) -> bool:
        return any(result.status == CheckStatus.FAIL for result in results)

    @staticmethod
    def _okx_hint(exc: Exception) -> str:
        text = str(exc)
        if "401" in text or "50113" in text:
            return "authentication failed; check key, secret, passphrase, IP binding, and demo/live mode"
        if "429" in text or "50011" in text:
            return "rate limited; wait and retry"
        if "502" in text or "timeout" in text.lower() or "network" in text.lower():
            return "exchange/network error; check OKX_BASE_URL, regional network/proxy, or retry later"
        return text[:240]


def format_results(results: list[CheckResult]) -> str:
    return "\n".join(result.line() for result in results)
