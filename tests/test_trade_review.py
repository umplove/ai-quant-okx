import unittest

from okx_quant_bot.models import MarketTicker, Position
from okx_quant_bot.trade_review import TradeReviewEngine


class TradeReviewTests(unittest.TestCase):
    def test_mark_to_market_reviews_open_position(self):
        position = Position("BTC-USDT", base_qty=2, avg_entry_price=100, highest_price=100)
        ticker = MarketTicker("BTC-USDT", 110, 100, 111, 99, 1000000, 1)

        reviews = TradeReviewEngine().mark_to_market([position], [ticker])

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].symbol, "BTC-USDT")
        self.assertAlmostEqual(reviews[0].pnl_usdt, 20)
        self.assertAlmostEqual(reviews[0].return_pct, 10)
        self.assertIn("赚钱", reviews[0].summary)


if __name__ == "__main__":
    unittest.main()
