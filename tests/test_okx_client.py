import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.exchange.okx import DEFAULT_USER_AGENT, OkxRestClient
from tests.test_strategy_risk import settings_for


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps({"code": "0", "data": []}).encode("utf-8")


class OkxRestClientTests(unittest.TestCase):
    def test_default_headers_include_user_agent_and_demo_flag(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return _FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            client = OkxRestClient(settings, timeout=3.5)

            with patch("urllib.request.urlopen", fake_urlopen):
                client.get_candles("BTC-USDT", limit=1)

        self.assertEqual(captured["headers"]["User-agent"], DEFAULT_USER_AGENT)
        self.assertEqual(captured["headers"]["X-simulated-trading"], "1")
        self.assertEqual(captured["timeout"], 3.5)

    def test_get_market_tickers_parses_okx_rows(self):
        payload = {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT",
                    "last": "120",
                    "open24h": "100",
                    "high24h": "130",
                    "low24h": "90",
                    "volCcy24h": "123456",
                    "ts": "1",
                }
            ],
        }

        def fake_urlopen(request, timeout):
            return _JsonResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                tickers = client.get_market_tickers()

        self.assertEqual(tickers[0].symbol, "BTC-USDT")
        self.assertAlmostEqual(tickers[0].change_pct_24h, 0.20)

    def test_place_stop_loss_order_posts_algo_payload(self):
        captured = {}
        payload = {"code": "0", "data": [{"algoId": "algo-1", "sCode": "0"}]}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JsonResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                result = client.place_stop_loss_order("BTC-USDT", 0.5, 80000)

        self.assertTrue(result.ok)
        self.assertEqual(captured["url"].split("?")[0], "https://www.okx.com/api/v5/trade/order-algo")
        self.assertEqual(captured["body"]["side"], "sell")
        self.assertEqual(captured["body"]["ordType"], "conditional")
        self.assertEqual(captured["body"]["slOrdPx"], "-1")

class _JsonResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
