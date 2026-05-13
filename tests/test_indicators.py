import unittest

from okx_quant_bot.strategy.indicators import ema, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_seeds_with_simple_average(self):
        values = [1, 2, 3, 4, 5]
        result = ema(values, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertEqual(result[2], 2)
        self.assertAlmostEqual(result[-1], 4.0)

    def test_rsi_handles_all_gains(self):
        values = list(range(1, 20))
        result = rsi(values, 14)
        self.assertEqual(result[14], 100.0)

    def test_rsi_handles_losses(self):
        values = [20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 7]
        result = rsi(values, 14)
        self.assertLess(result[14], 1)
        self.assertGreater(result[15], result[14])


if __name__ == "__main__":
    unittest.main()

