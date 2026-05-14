import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_quant_bot.exchange.okx import DEFAULT_USER_AGENT, OkxRestClient
from okx_quant_bot.models import Side
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

    def test_place_limit_buy_quote_posts_base_size_limit_payload(self):
        captured = {}
        payload = {"code": "0", "data": [{"ordId": "order-1", "sCode": "0"}]}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JsonResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                request, result = client.place_limit_buy_quote("BTC-USDT", 1000, 50000, "test")

        self.assertTrue(result.ok)
        self.assertEqual(request.order_type, "limit")
        self.assertEqual(captured["url"].split("?")[0], "https://www.okx.com/api/v5/trade/order")
        self.assertEqual(captured["body"]["side"], "buy")
        self.assertEqual(captured["body"]["ordType"], "limit")
        self.assertEqual(captured["body"]["px"], "50000")
        self.assertEqual(captured["body"]["sz"], "0.02")

    def test_limit_order_uses_instrument_precision(self):
        captured = {}
        responses = [
            {
                "code": "0",
                "data": [{"instId": "BTC-USDT", "minSz": "0.001", "lotSz": "0.001", "tickSz": "0.1"}],
            },
            {"code": "0", "data": [{"ordId": "order-1", "sCode": "0"}]},
        ]

        def fake_urlopen(request, timeout):
            payload = responses.pop(0)
            if request.full_url.endswith("/api/v5/trade/order"):
                captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JsonResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                request, result = client.place_limit_buy_quote("BTC-USDT", 1000, 50000.123, "test")

        self.assertTrue(result.ok)
        self.assertEqual(request.order_type, "limit")
        self.assertEqual(captured["body"]["px"], "50000.1")
        self.assertEqual(captured["body"]["sz"], "0.019")

    def test_limit_order_rejects_size_below_minimum(self):
        responses = [
            {
                "code": "0",
                "data": [{"instId": "BTC-USDT", "minSz": "0.001", "lotSz": "0.001", "tickSz": "0.1"}],
            }
        ]

        def fake_urlopen(request, timeout):
            return _JsonResponse(responses.pop(0))

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                request, result = client.place_limit_buy_quote("BTC-USDT", 10, 50000, "test")

        self.assertEqual(request.order_type, "limit")
        self.assertFalse(result.ok)
        self.assertIn("minSz", result.error)

    def test_cancel_order_posts_cancel_payload(self):
        captured = {}
        payload = {"code": "0", "data": [{"ordId": "order-1", "sCode": "0"}]}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _JsonResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                result = client.cancel_order("BTC-USDT", "order-1")

        self.assertEqual(result["code"], "0")
        self.assertEqual(captured["url"].split("?")[0], "https://www.okx.com/api/v5/trade/cancel-order")
        self.assertEqual(captured["body"], {"instId": "BTC-USDT", "ordId": "order-1"})

    def test_list_open_orders_uses_spot_pending_orders_endpoint(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return _JsonResponse({"code": "0", "data": []})

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                result = client.list_open_orders("BTC-USDT")

        self.assertEqual(result["code"], "0")
        self.assertIn("/api/v5/trade/orders-pending", captured["url"])
        self.assertIn("instType=SPOT", captured["url"])
        self.assertIn("instId=BTC-USDT", captured["url"])

    def test_swap_order_sets_leverage_pos_side_and_reduce_only(self):
        captured = []
        responses = [
            {"code": "0", "data": [{"sCode": "0"}]},
            {
                "code": "0",
                "data": [{"instId": "BTC-USDT-SWAP", "minSz": "1", "lotSz": "1", "tickSz": "0.1", "ctVal": "0.01", "ctValCcy": "BTC"}],
            },
            {"code": "0", "data": [{"ordId": "order-1", "sCode": "0"}]},
        ]

        def fake_urlopen(request, timeout):
            if request.data:
                captured.append((request.full_url, json.loads(request.data.decode("utf-8"))))
            return _JsonResponse(responses.pop(0))

        with tempfile.TemporaryDirectory() as tmp:
            client = OkxRestClient(settings_for(Path(tmp) / "bot.sqlite3"))
            with patch("urllib.request.urlopen", fake_urlopen):
                request, result = client.place_swap_market(
                    "BTC-USDT-SWAP",
                    Side.BUY,
                    3.9,
                    "long",
                    5,
                    "isolated",
                    "test",
                    reduce_only=True,
                )

        self.assertTrue(result.ok)
        self.assertEqual(request.market_type, "SWAP")
        self.assertEqual(captured[0][0].split("?")[0], "https://www.okx.com/api/v5/account/set-leverage")
        self.assertEqual(captured[0][1]["lever"], "5")
        self.assertEqual(captured[0][1]["posSide"], "long")
        self.assertEqual(captured[1][1]["tdMode"], "isolated")
        self.assertEqual(captured[1][1]["posSide"], "long")
        self.assertEqual(captured[1][1]["reduceOnly"], "true")
        self.assertEqual(captured[1][1]["sz"], "3")

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
