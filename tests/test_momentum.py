import tempfile
import unittest
from pathlib import Path

from okx_quant_bot.config import Settings
from okx_quant_bot.models import InfoSignal, MarketTicker
from okx_quant_bot.momentum import (
    CandidateScorer,
    MarketScanner,
    _is_crypto_market,
    stop_loss_plan,
    target_position_usdt,
)
from tests.test_strategy_risk import settings_for


class _FakeExchange:
    def __init__(self, tickers):
        self.tickers = tickers

    def get_market_tickers(self, inst_type="SPOT"):
        return self.tickers


class MomentumTests(unittest.TestCase):
    def test_market_scanner_uses_gainers_then_largest_amplitude(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _with(
                settings_for(Path(tmp) / "bot.sqlite3"),
                candidate_top_n=2,
                symbols=("AAA-USDT", "BBB-USDT", "CCC-USDT"),
            )
            tickers = [
                MarketTicker("AAA-USDT", 150, 100, 160, 140, 1000000, 1),
                MarketTicker("BBB-USDT", 140, 100, 170, 90, 900000, 1),
                MarketTicker("CCC-USDT", 110, 100, 190, 95, 800000, 1),
                MarketTicker("USDC-USDT", 1, 1, 1, 1, 1000000, 1),
            ]

            ranked = MarketScanner(_FakeExchange(tickers), settings).top_momentum_tickers()

        self.assertEqual([ticker.symbol for ticker in ranked], ["BBB-USDT", "AAA-USDT"])

    def test_market_scanner_only_uses_configured_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _with(settings_for(Path(tmp) / "bot.sqlite3"), symbols=("BTC-USDT",))
            tickers = [
                MarketTicker("BTC-USDT", 120, 100, 130, 90, 1000000, 1),
                MarketTicker("SAHARA-USDT", 120, 100, 130, 90, 1000000, 1),
            ]

            ranked = MarketScanner(_FakeExchange(tickers), settings).top_momentum_tickers()

        self.assertEqual([ticker.symbol for ticker in ranked], ["BTC-USDT"])

    def test_candidate_can_trade_without_info_confirmation(self):
        ticker = MarketTicker("BTC-USDT", 120, 100, 130, 95, 10_000_000, 1)

        without_info = CandidateScorer().score([ticker], [])
        with_info = CandidateScorer().score(
            [ticker],
            [InfoSignal("news", "BTC-USDT", 1.0, "Bitcoin breaks higher")],
        )

        self.assertTrue(without_info[0].confirmed)
        self.assertTrue(with_info[0].confirmed)
        self.assertGreater(with_info[0].total_score, without_info[0].total_score)

    def test_candidate_can_still_require_news_confirmation(self):
        ticker = MarketTicker("BTC-USDT", 120, 100, 130, 95, 10_000_000, 1)

        candidate = CandidateScorer(require_info_confirmation=True).score([ticker], [])[0]

        self.assertFalse(candidate.confirmed)

    def test_polymarket_confirmation_must_match_token(self):
        self.assertTrue(_is_crypto_market("Will Bitcoin hit a new high?", "BTC"))
        self.assertFalse(_is_crypto_market("Will crypto markets rally this week?", "NAVX"))

    def test_percent_position_size_targets_200_usdt_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp) / "bot.sqlite3")
            plan = stop_loss_plan(settings, "BTC-USDT", entry_price=100.0, quote_amount=target_position_usdt(settings))

        self.assertEqual(target_position_usdt(settings), 1000.0)
        self.assertAlmostEqual(plan.stop_price, 80.0)
        self.assertAlmostEqual(plan.risk_usdt, 200.0)

    def test_fixed_loss_stop_price_risks_200_usdt_for_any_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _with(settings_for(Path(tmp) / "bot.sqlite3"), stop_mode="fixed_loss")
            plan = stop_loss_plan(settings, "ETH-USDT", entry_price=100.0, quote_amount=500.0)

        self.assertAlmostEqual(plan.size, 5.0)
        self.assertAlmostEqual(plan.stop_price, 60.0)
        self.assertAlmostEqual((plan.entry_price - plan.stop_price) * plan.size, 200.0)


def _with(settings: Settings, **updates) -> Settings:
    return settings.__class__(**{**settings.__dict__, **updates})


if __name__ == "__main__":
    unittest.main()
