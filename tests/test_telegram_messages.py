import unittest

from okx_quant_bot.doctor import TELEGRAM_TEST_MESSAGE
from okx_quant_bot.models import Side
from okx_quant_bot.runner import (
    _entry_blocked_message,
    _order_failed_message,
    _order_recorded_message,
    _runtime_error_message,
    _start_message,
)


class TelegramMessageTests(unittest.TestCase):
    def test_doctor_test_message_is_chinese(self):
        self.assertEqual(TELEGRAM_TEST_MESSAGE, "OKX量化机器人连通性检查通过")

    def test_runner_notifications_are_chinese(self):
        self.assertEqual(_start_message(True), "OKX量化机器人已启动（模拟盘模式）。")
        self.assertIn("已暂停", _runtime_error_message("BTC-USDT", RuntimeError("boom")))
        self.assertIn("开仓被风控拦截", _entry_blocked_message("BTC-USDT", "no_cash_available"))
        self.assertIn("可用现金不足", _entry_blocked_message("BTC-USDT", "no_cash_available"))
        self.assertIn("下单失败", _order_failed_message("ETH-USDT", "余额不足"))
        self.assertIn(
            "买入已记录",
            _order_recorded_message(
                "BTC-USDT",
                Side.BUY,
                "trend_ok_reclaim_ema_fast_rsi_rebound",
            ),
        )
        self.assertIn(
            "趋势向上，价格收复快线，RSI反弹",
            _order_recorded_message(
                "BTC-USDT",
                Side.BUY,
                "trend_ok_reclaim_ema_fast_rsi_rebound",
            ),
        )


if __name__ == "__main__":
    unittest.main()
