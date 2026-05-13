import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.intelligence import IntelligenceRadar
from tests.test_strategy_risk import settings_for


class _Response:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.text.encode("utf-8")


class IntelligenceTests(unittest.TestCase):
    def test_rss_items_become_signals(self):
        feed = """
        <rss><channel><item>
          <title>Bitcoin ETF listing catalyst</title>
          <link>https://example.test/btc</link>
        </item></channel></rss>
        """
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{**settings.__dict__, "news_rss_urls": ("https://feed.test/rss",)}
            )
            with patch("urllib.request.urlopen", return_value=_Response(feed)):
                scan = IntelligenceRadar(settings).scan(("BTC-USDT", "ETH-USDT"))

        self.assertEqual(len(scan.items), 1)
        self.assertEqual(scan.items[0].symbol, "BTC-USDT")
        self.assertGreater(scan.signals[0].score, 1)

    def test_cryptopanic_items_match_currencies(self):
        payload = {
            "results": [
                {
                    "title": "Ethereum mainnet upgrade",
                    "url": "https://example.test/eth",
                    "currencies": [{"code": "ETH"}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            settings = settings.__class__(
                **{**settings.__dict__, "cryptopanic_auth_token": "token"}
            )
            with patch("urllib.request.urlopen", return_value=_Response(json.dumps(payload))):
                scan = IntelligenceRadar(settings).scan(("BTC-USDT", "ETH-USDT"))

        self.assertEqual(scan.items[0].source, "cryptopanic")
        self.assertEqual(scan.items[0].symbol, "ETH-USDT")


if __name__ == "__main__":
    unittest.main()
